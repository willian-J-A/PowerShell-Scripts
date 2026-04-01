from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pathlib import Path
import os
import json
import re
import subprocess
import unicodedata

app = FastAPI()

BRIDGE_TOKEN = os.getenv("OPENCLAW_BRIDGE_TOKEN", "")
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT_NAME", "main")
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "openclaw")
OPENCLAW_TIMEOUT = int(os.getenv("OPENCLAW_TIMEOUT_SECONDS", "120"))
RULES_PATH = Path(os.getenv("DECISION_RULES_PATH", "config/decision_rules.json"))
BOT_RULES_PATH = Path(os.getenv("BOT_RULES_PATH", "config/bot_rules.md"))
KB_BASE_PATH = Path(os.getenv("KB_BASE_PATH", "kb/clientes"))

ALLOWED_STATUS = {
    "INICIO", "IDENTIFICACAO", "TRIAGEM", "BASE_CONHECIMENTO", "FILA_N1",
    "SOLUCAO_PROPOSTA", "AGUARDANDO_VALIDACAO", "AGENDAMENTO",
    "AGUARDANDO_RESPOSTA", "RESERVA_DATA", "ENCERRADO"
}


class ContactPayload(BaseModel):
    contactId: Optional[str] = ""
    serviceId: Optional[str] = ""
    ambiguous_contact: bool = False
    contact_count: int = 0


class ContextPayload(BaseModel):
    protocolo: str = ""
    numero: str = ""
    nome: str = ""
    empresa: str = ""
    identificado: bool = False
    triagem_concluida: bool = False
    tentativas: int = 0
    ticket_aberto: bool = False
    agendamento_enviado: bool = False
    encerrado: bool = False
    updated_at: str = ""
    last_status: str = ""
    last_intent: str = ""
    last_event_type: str = ""
    ultima_solicitacao_tipo: str = ""


class KBItem(BaseModel):
    id: str
    intent: str
    resposta: str
    ativo: bool = True


class DecisionRequest(BaseModel):
    protocol: str
    customer_id: str
    customer_name: Optional[str] = ""
    message_text: Optional[str] = ""
    event_type: Optional[str] = "message"
    contact: ContactPayload
    contexto: ContextPayload
    kb_active: List[KBItem] = Field(default_factory=list)


class RuntimeFileCache:
    def __init__(self):
        self._cache: Dict[str, Any] = {}

    def load_json(self, path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._cache[key] = {"mtime": None, "content": default}
            return default

        cached = self._cache.get(key)
        if cached and cached.get("mtime") == mtime:
            return cached.get("content", default)

        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(content, dict):
                content = default
        except Exception:
            content = default

        self._cache[key] = {"mtime": mtime, "content": content}
        return content

    def load_text(self, path: Path, default: str) -> str:
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._cache[key] = {"mtime": None, "content": default}
            return default

        cached = self._cache.get(key)
        if cached and cached.get("mtime") == mtime:
            return str(cached.get("content", default))

        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            content = default

        self._cache[key] = {"mtime": mtime, "content": content}
        return content


runtime_cache = RuntimeFileCache()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def normalize_text(text: str) -> str:
    return ' '.join((text or '').strip().lower().split())


def strip_accents(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', text or '') if unicodedata.category(c) != 'Mn')


def normalize_slug(text: str) -> str:
    base = strip_accents(text).lower().strip()
    base = re.sub(r'[^a-z0-9]+', '_', base).strip('_')
    return base or 'default'


def load_decision_rules() -> Dict[str, Any]:
    defaults = {
        "tech_names": ["william", "willian", "breno", "erivelton"],
        "tech_aliases": {"wiliam": "william", "wilian": "willian", "eriveton": "erivelton"},
        "greetings": ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite"],
        "irritation_terms": ["demorando", "ninguem resolve", "frustrado", "irritado", "bloqueado", "sem acesso"],
        "handoff": {
            "require_frustration_for_tech": True,
            "frustration_requires_blockage": False,
            "open_ticket_when_handoff": True,
            "message_frustrated": "[BOT] Entendo a sua frustração 🙂 Vou acionar um atendente para continuar com você.",
            "message_soft_transition": "[BOT] Claro 🙂 Vou verificar a disponibilidade dele. Se preferir, já posso te orientar por aqui ou seguir com o agendamento.",
        },
        "greeting_message": "[BOT] Olá 🙂 Posso te ajudar por aqui. Me conta o que você precisa.",
        "scheduling": {"allow_on_soft_tech_request": True},
    }
    return runtime_cache.load_json(RULES_PATH, defaults)


def load_bot_rules() -> str:
    default_text = "Responda de forma cordial, clara, humana e objetiva."
    return runtime_cache.load_text(BOT_RULES_PATH, default_text)


def load_client_kb(req: DecisionRequest) -> Dict[str, Any]:
    KB_BASE_PATH.mkdir(parents=True, exist_ok=True)
    slug = normalize_slug(req.customer_name or req.contexto.empresa or "default")
    kb_path = KB_BASE_PATH / f"{slug}.json"
    default_path = KB_BASE_PATH / "default.json"
    default_kb = {
        "cliente": slug,
        "escopo": "geral",
        "restricoes": [],
        "regras_especificas": [],
        "respostas_conhecidas": {},
    }
    if kb_path.exists():
        return runtime_cache.load_json(kb_path, default_kb)
    return runtime_cache.load_json(default_path, default_kb)


def levenshtein_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


def fuzzy_contains_tech_name(text: str, rules: Dict[str, Any]) -> bool:
    base = strip_accents(text).lower()
    tokens = re.findall(r"[a-z]+", base)
    aliases = rules.get("tech_aliases", {}) or {}
    names = [strip_accents(str(n)).lower() for n in rules.get("tech_names", [])]

    if any(name in base for name in names):
        return True

    for token in [aliases.get(t, t) for t in tokens]:
        for target in names:
            if abs(len(token) - len(target)) <= 1 and levenshtein_distance(token, target) <= 1:
                return True
    return False


def detect_signals(req: DecisionRequest, rules: Dict[str, Any]) -> Dict[str, Any]:
    text = normalize_text(req.message_text or '')
    text_no_accent = strip_accents(text)
    greetings = {strip_accents(str(x)).lower() for x in rules.get("greetings", [])}
    irritation_terms = [strip_accents(str(x)).lower() for x in rules.get("irritation_terms", [])]

    return {
        "text": text,
        "text_no_accent": text_no_accent,
        "only_greeting": text_no_accent in greetings,
        "pediu_tecnico_especifico": fuzzy_contains_tech_name(text, rules),
        "cliente_irritado": any(term in text_no_accent for term in irritation_terms),
        "bloqueio_real": any(term in text_no_accent for term in ["bloque", "sem acesso", "nao consigo", "não consigo"]),
        "ambiguous_contact": bool(req.contact.ambiguous_contact),
        "closed_event": req.event_type in {"closed", "conversation_closed", "finalized"} or bool(req.contexto.encerrado),
    }


def infer_request_type(status: str, mensagem: str, previous: str) -> str:
    if status == "IDENTIFICACAO":
        return "identificacao"
    if status in {"TRIAGEM", "BASE_CONHECIMENTO", "SOLUCAO_PROPOSTA", "AGUARDANDO_VALIDACAO"} and "?" in mensagem:
        return "triagem"
    if status == "AGENDAMENTO":
        return "agendamento"
    if status == "FILA_N1":
        return "handoff"
    return previous


def build_decision(req: DecisionRequest, *, status: str, intent: str, mensagem: str, abrir_ticket: bool, agendar: bool, handoff_humano: bool, confidence: float, source: str) -> Dict[str, Any]:
    updated = {
        **req.contexto.model_dump(),
        "protocolo": req.protocol,
        "numero": req.customer_id,
        "updated_at": now_iso(),
        "last_status": status,
        "last_intent": intent,
        "last_event_type": req.event_type,
        "ultima_solicitacao_tipo": infer_request_type(status, mensagem, req.contexto.ultima_solicitacao_tipo or ""),
        "ticket_aberto": bool(req.contexto.ticket_aberto or abrir_ticket),
        "agendamento_enviado": bool(req.contexto.agendamento_enviado or agendar),
        "encerrado": bool(req.contexto.encerrado or status == "ENCERRADO"),
    }
    return {
        "decisao": {
            "status": status,
            "intent": intent,
            "mensagem": mensagem,
            "confidence": confidence,
            "abrir_ticket": abrir_ticket,
            "agendar": agendar,
            "handoff_humano": handoff_humano,
        },
        "contexto_atualizado": updated,
        "meta": {"source": source, "fallback_used": source != "openclaw_cli_v2_1"},
    }


def hard_rules_decision(req: DecisionRequest, signals: Dict[str, Any], rules: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    handoff_cfg = rules.get("handoff", {}) or {}
    greeting_message = rules.get("greeting_message") or "[BOT] Olá 🙂 Posso te ajudar por aqui. Me conta o que você precisa."

    if signals["closed_event"]:
        return build_decision(req, status="ENCERRADO", intent="closed_cleanup", mensagem="[BOT] Atendimento finalizado.", abrir_ticket=False, agendar=False, handoff_humano=False, confidence=0.99, source="hard_rules")

    if signals["only_greeting"]:
        return build_decision(req, status="INICIO", intent="saudacao_inicial", mensagem=greeting_message, abrir_ticket=False, agendar=False, handoff_humano=False, confidence=0.99, source="hard_rules")

    if signals["ambiguous_contact"]:
        return build_decision(req, status="IDENTIFICACAO", intent="identificacao", mensagem="[BOT] Para seguir com segurança, me confirme seu nome completo e empresa 🙂", abrir_ticket=False, agendar=False, handoff_humano=False, confidence=0.98, source="hard_rules")

    if signals["pediu_tecnico_especifico"]:
        frustrated = signals["cliente_irritado"] or signals["bloqueio_real"]
        if frustrated:
            return build_decision(
                req,
                status="FILA_N1",
                intent="handoff_humano",
                mensagem=str(handoff_cfg.get("message_frustrated") or "[BOT] Entendo a sua frustração 🙂 Vou acionar um atendente para continuar com você."),
                abrir_ticket=bool(handoff_cfg.get("open_ticket_when_handoff", True)),
                agendar=False,
                handoff_humano=True,
                confidence=0.99,
                source="hard_rules",
            )

        return build_decision(
            req,
            status="AGENDAMENTO" if bool((rules.get("scheduling", {}) or {}).get("allow_on_soft_tech_request", True)) else "TRIAGEM",
            intent="preferencia_tecnico",
            mensagem=str(handoff_cfg.get("message_soft_transition") or "[BOT] Claro 🙂 Vou verificar a disponibilidade dele. Se preferir, já posso te orientar por aqui ou seguir com o agendamento."),
            abrir_ticket=False,
            agendar=bool((rules.get("scheduling", {}) or {}).get("allow_on_soft_tech_request", True)),
            handoff_humano=False,
            confidence=0.95,
            source="hard_rules",
        )

    return None


def build_prompt(req: DecisionRequest, signals: Dict[str, Any], rules: Dict[str, Any], bot_rules: str, kb: Dict[str, Any]) -> str:
    payload = {
        "protocol": req.protocol,
        "customer_id": req.customer_id,
        "customer_name": req.customer_name,
        "message_text": req.message_text,
        "event_type": req.event_type,
        "contact": req.contact.model_dump(),
        "contexto": req.contexto.model_dump(),
        "signals": signals,
        "decision_policy_excerpt": {
            "greetings": rules.get("greetings", []),
            "handoff": rules.get("handoff", {}),
            "scheduling": rules.get("scheduling", {}),
        },
        "kb_cliente": kb,
    }
    return f"""Você interpreta mensagens para um workflow DigiSAC e retorna SOMENTE JSON válido.

Instruções de comportamento (bot_rules):
{bot_rules}

Restrições operacionais:
- Não afirme execução de ações.
- Inicie mensagem com [BOT].
- Preserve o contrato JSON já definido.

Contexto:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()


def extract_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def validate_ai_response(obj: Dict[str, Any]) -> Dict[str, Any]:
    required = [
        "status_sugerido", "intent", "mensagem", "confidence",
        "recomenda_abrir_ticket", "recomenda_handoff_humano", "recomenda_agendamento", "motivo"
    ]
    if not all(k in obj for k in required):
        raise ValueError("missing required keys")
    if obj["status_sugerido"] not in ALLOWED_STATUS:
        raise ValueError("invalid status_sugerido")
    if not str(obj["mensagem"]).startswith("[BOT]"):
        raise ValueError("mensagem must start with [BOT]")
    conf = float(obj["confidence"])
    if conf < 0 or conf > 1:
        raise ValueError("confidence out of range")
    return obj


def map_to_bridge_response(req: DecisionRequest, ai: Dict[str, Any], signals: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    # Guardrails operacionais: hard rules sempre prevalecem.
    hard = hard_rules_decision(req, signals, rules)
    if hard:
        return hard

    status = ai.get("status_sugerido") or "TRIAGEM"
    intent = ai.get("intent") or "fallback_local"
    mensagem = ai.get("mensagem", "")
    handoff = bool(ai.get("recomenda_handoff_humano", False))
    abrir_ticket = bool(ai.get("recomenda_abrir_ticket", False))
    agendar = bool(ai.get("recomenda_agendamento", False))

    if handoff:
        status = "FILA_N1"
        intent = "handoff_humano"
        abrir_ticket = bool((rules.get("handoff", {}) or {}).get("open_ticket_when_handoff", True))
        agendar = False

    if status == "AGENDAMENTO":
        agendar = True
        handoff = False

    return build_decision(
        req,
        status=status,
        intent=intent,
        mensagem=mensagem,
        abrir_ticket=abrir_ticket,
        agendar=agendar,
        handoff_humano=handoff,
        confidence=float(ai.get("confidence", 0.5)),
        source="openclaw_cli_v2_1",
    )


def call_openclaw(req: DecisionRequest) -> Dict[str, Any]:
    rules = load_decision_rules()
    bot_rules = load_bot_rules()
    kb_cliente = load_client_kb(req)
    signals = detect_signals(req, rules)

    hard = hard_rules_decision(req, signals, rules)
    if hard:
        return hard

    prompt = build_prompt(req, signals, rules, bot_rules, kb_cliente)
    session_id = f"digisac-{req.protocol}"
    cmd = [OPENCLAW_BIN, "agent", "--agent", OPENCLAW_AGENT, "--session-id", session_id, "--message", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=OPENCLAW_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"openclaw exited with {result.returncode}")
    ai = validate_ai_response(extract_json(result.stdout))
    return map_to_bridge_response(req, ai, signals, rules)


def fallback_response(req: DecisionRequest) -> Dict[str, Any]:
    rules = load_decision_rules()
    signals = detect_signals(req, rules)
    hard = hard_rules_decision(req, signals, rules)
    if hard:
        return hard
    return build_decision(
        req,
        status="TRIAGEM",
        intent="fallback_local",
        mensagem="[BOT] Perfeito 🙂 Me conta um pouco mais do que está acontecendo para eu te ajudar melhor.",
        abrir_ticket=False,
        agendar=False,
        handoff_humano=False,
        confidence=0.55,
        source="bridge_fallback_v2_1",
    )


@app.get("/health")
def health():
    return {"ok": True, "service": "openclaw-decision-bridge", "mode": "openclaw-cli-v2_1"}


@app.post("/decision/openclaw")
def decision_openclaw(payload: DecisionRequest, authorization: Optional[str] = Header(default="")):
    if BRIDGE_TOKEN and authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")

    # isolamento: IA nunca altera o core; somente lê regras/KB externas.
    try:
        return call_openclaw(payload)
    except Exception:
        return fallback_response(payload)
