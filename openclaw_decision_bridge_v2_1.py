from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import unicodedata
import copy
import threading

app = FastAPI()

BRIDGE_TOKEN = os.getenv("OPENCLAW_BRIDGE_TOKEN", "")
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT_NAME", "main")
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "openclaw")
OPENCLAW_TIMEOUT = int(os.getenv("OPENCLAW_TIMEOUT_SECONDS", "120"))
RULES_PATH = Path(os.getenv("DECISION_RULES_PATH", "config/decision_rules.json"))
BOT_RULES_PATH = Path(os.getenv("BOT_RULES_PATH", "config/bot_rules.md"))
KB_BASE_PATH = Path(os.getenv("KB_BASE_PATH", "kb/clientes"))
STATE_BASE_PATH = Path(os.getenv("PROTOCOL_STATE_PATH", "runtime/protocols"))

STATE_VERSION = "v3"
SHORT_HISTORY_LIMIT = int(os.getenv("SHORT_HISTORY_LIMIT", "12"))
_LOCKS_GUARD = threading.Lock()
_PROTOCOL_LOCKS: Dict[str, threading.RLock] = {}

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


class MessageMetaPayload(BaseModel):
    conversation_id: Optional[str] = ""
    ticket_id: Optional[str] = ""
    timestamp: Optional[str] = ""
    author: Optional[str] = "customer"
    is_automatic: bool = False


class KBItem(BaseModel):
    id: str
    intent: str
    resposta: str
    ativo: bool = True


class DecisionRequest(BaseModel):
    protocol: str
    customer_id: str
    customer_name: Optional[str] = ""
    customer_company: Optional[str] = ""
    message_text: Optional[str] = ""
    event_type: Optional[str] = "message"
    channel: Optional[str] = "digisac"
    contact: ContactPayload
    message_meta: Optional[MessageMetaPayload] = None
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
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text or "") if unicodedata.category(c) != "Mn")


def normalize_slug(text: str) -> str:
    base = strip_accents(text).lower().strip()
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return base or "default"


def load_decision_rules() -> Dict[str, Any]:
    defaults = {
        "tech_names": ["william", "willian", "breno", "erivelton"],
        "tech_aliases": {"wiliam": "william", "wilian": "willian", "eriveton": "erivelton"},
        "greetings": ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite"],
        "irritation_terms": ["demorando", "ninguem resolve", "frustrado", "irritado", "bloqueado", "sem acesso"],
        "handoff": {
            "open_ticket_when_handoff": True,
            "message_frustrated": "[BOT] Entendo a sua frustração 🙂 Vou acionar um atendente para continuar com você.",
            "message_soft_transition": "[BOT] Claro 🙂 Vou verificar a disponibilidade dele. Se preferir, já posso te orientar por aqui ou seguir com o agendamento.",
        },
        "scheduling": {"allow_on_soft_tech_request": True},
        "greeting_message": "[BOT] Olá 🙂 Posso te ajudar por aqui. Me conta o que você precisa.",
        "anti_loop": {
            "enabled": True,
            "fallback_variations": [
                "[BOT] Entendi 🙂 Para avançar, você usa essa impressora por USB ou rede/Wi‑Fi?",
                "[BOT] Perfeito 🙂 Isso acontece sempre ou só em alguns momentos?",
                "[BOT] Certo 🙂 Você tentou reiniciar o equipamento e o computador?"
            ]
        },
        "force_handoff_terms": [
            "quero falar com atendente",
            "quero falar com uma pessoa",
            "me transfere",
            "quero um humano",
            "nao quero falar com robo",
            "não quero falar com robô"
        ],
        "repeat_weights": {
            "should_change_path": 1,
            "same_diagnostic_path": 1,
            "user_answered_previous": 1,
            "same_intent_high": 2,
            "same_intent_medium": 1,
            "repeated_collect": 1
        },
        "repeat_thresholds": {"light": 1, "medium": 2, "high": 4},
        "formatting": {"max_emojis": 1},
    }
    return runtime_cache.load_json(RULES_PATH, defaults)


def load_bot_rules() -> str:
    return runtime_cache.load_text(BOT_RULES_PATH, "Responda de forma cordial, clara, humana e objetiva.")


def load_client_data(req: DecisionRequest) -> Dict[str, Any]:
    KB_BASE_PATH.mkdir(parents=True, exist_ok=True)
    slug = normalize_slug(req.customer_name or req.customer_company or "default")
    default_root = KB_BASE_PATH / "default"
    client_root = KB_BASE_PATH / slug

    rules_default = {
        "allow_scheduling": True,
        "scope": "geral",
        "restrictions": [],
        "exclusions": [],
        "owners": []
    }
    kb_default = {
        "cliente": slug,
        "escopo": "geral",
        "restricoes": [],
        "regras_especificas": [],
        "respostas_conhecidas": {}
    }

    rules = runtime_cache.load_json(client_root / "rules.json", runtime_cache.load_json(default_root / "rules.json", rules_default))
    kb = runtime_cache.load_json(client_root / "kb.json", runtime_cache.load_json(default_root / "kb.json", kb_default))
    return {"slug": slug, "rules": rules, "kb": kb}


def protocol_state_path(protocol: str) -> Path:
    safe_protocol = re.sub(r"[^0-9A-Za-z_-]+", "_", protocol)
    return STATE_BASE_PATH / f"{safe_protocol}.json"


def protocol_lock(protocol: str) -> threading.RLock:
    with _LOCKS_GUARD:
        if protocol not in _PROTOCOL_LOCKS:
            _PROTOCOL_LOCKS[protocol] = threading.RLock()
        return _PROTOCOL_LOCKS[protocol]


def new_state(req: DecisionRequest) -> Dict[str, Any]:
    now = now_iso()
    return {
        "protocol": req.protocol,
        "is_open": True,
        "created_at": now,
        "updated_at": now,
        "customer": {
            "id": req.customer_id,
            "name": req.customer_name or "",
            "company": req.customer_company or "",
        },
        "conversation": {
            "last_user_message": "",
            "last_bot_message": "",
            "ultima_pergunta_bot": "",
            "ultima_resposta_cliente": "",
            "historico_curto": [],
            "historico_completo": [],
        },
        "workflow": {
            "status": "INICIO",
            "intent": "",
            "tentativas": 0,
            "respostas_uteis_cliente": 0,
            "tentativas_mesma_intencao": 0,
            "coletas_repetidas": 0,
            "tentativas_falhas": [],
            "feedback": {"sucesso": 0, "falha": 0},
            "triagem_concluida": False,
            "encerrado": False,
            "aguardando_validacao": False,
            "ultima_solicitacao_tipo": "",
        },
        "ticket": {"aberto": False, "ticket_id": None, "opened_at": None},
        "handoff": {"realizado": False, "motivo": None, "timestamp": None, "bot_silenciado": False},
        "agendamento": {"enviado": False, "timestamp": None},
        "identificacao": {
            "estado": "nao_iniciada",
            "nome_coletado": bool(req.customer_name),
            "empresa_coletada": bool(req.customer_company),
            "confianca": 0.6 if (req.customer_name or req.customer_company) else 0.0,
            "tentativas": 0,
        },
        "client_profile": {
            "kb_active": [],
        },
        "solution": {"proposta": False, "validada": False, "solucionado": False},
        "last_execution": {"input_payload": {}, "decision_output": {}, "actions_taken": [], "event_id": ""},
    }


def load_protocol_state(req: DecisionRequest) -> Dict[str, Any]:
    STATE_BASE_PATH.mkdir(parents=True, exist_ok=True)
    p = protocol_state_path(req.protocol)
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    return new_state(req)


def save_protocol_state(protocol: str, state: Dict[str, Any]) -> None:
    STATE_BASE_PATH.mkdir(parents=True, exist_ok=True)
    p = protocol_state_path(protocol)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def archive_protocol_state(protocol: str) -> None:
    p = protocol_state_path(protocol)
    if not p.exists():
        return
    arch = STATE_BASE_PATH / "archived"
    arch.mkdir(parents=True, exist_ok=True)
    target = arch / f"{protocol}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    p.rename(target)


def normalize_event(req: DecisionRequest) -> Dict[str, Any]:
    meta = req.message_meta.model_dump() if req.message_meta else {}
    text = (req.message_text or "").strip()
    timestamp = meta.get("timestamp") or now_iso()
    author = (meta.get("author") or "customer").lower()
    is_automatic = bool(meta.get("is_automatic", False)) or "mensagem automática" in strip_accents(text).lower()

    event_key = f"{req.protocol}|{req.customer_id}|{req.event_type}|{author}|{timestamp}|{normalize_text(text)}"
    event_id = hashlib.sha1(event_key.encode("utf-8")).hexdigest()

    return {
        "protocol": req.protocol,
        "customer_id": req.customer_id,
        "customer_name": req.customer_name or "",
        "customer_company": req.customer_company or "",
        "channel": req.channel or "digisac",
        "event_type": req.event_type or "message",
        "message_text": text,
        "timestamp": timestamp,
        "author": author,
        "is_automatic": is_automatic,
        "contact": req.contact.model_dump(),
        "message_meta": meta,
        "event_id": event_id,
        "raw_payload": req.model_dump(),
    }


def append_history(state: Dict[str, Any], role: str, text: str, timestamp: str, source: str) -> bool:
    item = {"role": role, "text": text, "timestamp": timestamp, "source": source}
    hist = state["conversation"]["historico_completo"]
    if hist and hist[-1].get("role") == role and normalize_text(hist[-1].get("text", "")) == normalize_text(text):
        return False
    hist.append(item)
    short = state["conversation"]["historico_curto"]
    short.append({"role": role, "text": text})
    if len(short) > SHORT_HISTORY_LIMIT:
        state["conversation"]["historico_curto"] = short[-SHORT_HISTORY_LIMIT:]
    return True


def is_useful_customer_text(text: str) -> bool:
    normalized = normalize_text(text)
    if len(normalized) < 3:
        return False
    low_value = {
        "ok", "blz", "sim", "nao", "não", "obrigado", "obrigada", "valeu", "show", "👍", "👍🏻", "👍🏽", "👍🏿"
    }
    return normalized not in low_value


def extract_kb_policy(state: Dict[str, Any]) -> Dict[str, Any]:
    kb_active = state.get("client_profile", {}).get("kb_active", [])
    policy: Dict[str, Any] = {}
    if isinstance(kb_active, list):
        for item in kb_active:
            if isinstance(item, dict):
                item_policy = item.get("policy")
                if isinstance(item_policy, dict):
                    policy.update(item_policy)
    return policy


def consolidate_state(state: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    state["updated_at"] = now_iso()
    state["customer"]["id"] = event["customer_id"]
    if event["customer_name"]:
        state["customer"]["name"] = event["customer_name"]
    if event["customer_company"]:
        state["customer"]["company"] = event["customer_company"]
    incoming_kb_active = event.get("raw_payload", {}).get("kb_active", [])
    if isinstance(incoming_kb_active, list) and incoming_kb_active:
        state["client_profile"]["kb_active"] = incoming_kb_active

    if not event["is_automatic"] and event["author"] in {"customer", "client", "usuario", "user"} and event["message_text"]:
        state["conversation"]["last_user_message"] = event["message_text"]
        state["conversation"]["ultima_resposta_cliente"] = event["message_text"]
        event["customer_turn_recorded"] = append_history(state, "user", event["message_text"], event["timestamp"], event["channel"])
        event["customer_turn_useful"] = is_useful_customer_text(event["message_text"])
        low_text = normalize_text(event["message_text"])
        if any(x in low_text for x in ["resolveu", "funcionou", "deu certo", "ok agora"]):
            state["workflow"]["feedback"]["sucesso"] = int(state["workflow"]["feedback"].get("sucesso", 0)) + 1
        if any(x in low_text for x in ["nao resolveu", "não resolveu", "continua", "mesmo problema", "nao funcionou", "não funcionou"]):
            state["workflow"]["feedback"]["falha"] = int(state["workflow"]["feedback"].get("falha", 0)) + 1
    else:
        event["customer_turn_recorded"] = False
        event["customer_turn_useful"] = False

    return state


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


def detect_signals(state: Dict[str, Any], event: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    text = normalize_text(event["message_text"])
    text_no_accent = strip_accents(text)
    greetings = {strip_accents(str(x)).lower() for x in rules.get("greetings", [])}
    irritation_terms = [strip_accents(str(x)).lower() for x in rules.get("irritation_terms", [])]
    force_handoff_terms = [strip_accents(str(x)).lower() for x in rules.get("force_handoff_terms", [])]

    return {
        "text": text,
        "text_no_accent": text_no_accent,
        "only_greeting": text_no_accent in greetings,
        "pediu_tecnico_especifico": fuzzy_contains_tech_name(text, rules),
        "cliente_irritado": any(term in text_no_accent for term in irritation_terms),
        "pedido_humano_explicito": any(term in text_no_accent for term in force_handoff_terms),
        "bloqueio_real": any(term in text_no_accent for term in ["bloque", "sem acesso", "nao consigo", "não consigo"]),
        "closed_event": event["event_type"] in {"closed", "conversation_closed", "finalized"} or bool(state["workflow"].get("encerrado")),
        "ambiguous_contact": bool(event["contact"].get("ambiguous_contact", False)),
        "has_diagnostic_hint": any(term in text_no_accent for term in ["nao funciona", "não funciona", "deslig", "erro", "impressora", "internet", "lento"]),
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
    return previous or ""


def apply_human_format(message: str, rules: Dict[str, Any]) -> str:
    text = (message or "").strip()
    if not text:
        text = "[BOT] Posso te ajudar por aqui."
    if not text.startswith("[BOT]"):
        text = f"[BOT] {text}"

    max_emojis = int(((rules.get("formatting", {}) or {}).get("max_emojis", 1)))
    if max_emojis <= 0:
        text = re.sub(r"[\U0001F300-\U0001FAFF]", "", text)
    else:
        emojis = list(re.finditer(r"[\U0001F300-\U0001FAFF]", text))
        for m in emojis[max_emojis:]:
            i = m.start()
            text = text[:i] + text[i + 1:]

    return re.sub(r"\s{2,}", " ", text).strip()


def anti_loop_guard(state: Dict[str, Any], message: str, rules: Dict[str, Any], signals: Dict[str, Any]) -> str:
    # Guardrail mínimo no backend: evitar apenas repetição literal imediata.
    prev = normalize_text(state["conversation"].get("last_bot_message", ""))
    curr = normalize_text(message)
    if prev and curr == prev:
        return "[BOT] Entendi. Vou seguir por outro caminho para avançar no atendimento."
    return message


def build_prompt(state: Dict[str, Any], event: Dict[str, Any], signals: Dict[str, Any], rules: Dict[str, Any], bot_rules: str, client_data: Dict[str, Any]) -> str:
    contexto = {
        "ultima_pergunta_bot": state["conversation"].get("ultima_pergunta_bot", ""),
        "ultima_resposta_cliente": state["conversation"].get("ultima_resposta_cliente", ""),
        "historico_curto": state["conversation"].get("historico_curto", []),
        "resumo_turno": {
            "last_status": state["workflow"].get("status", "INICIO"),
            "last_intent": state["workflow"].get("intent", ""),
            "ultima_solicitacao_tipo": state["workflow"].get("ultima_solicitacao_tipo", ""),
            "tentativas": state["workflow"].get("tentativas", 0),
        },
        "last_bot_message": normalize_text(state["conversation"].get("last_bot_message", "")),
    }

    payload = {
        "protocol": event["protocol"],
        "customer_id": event["customer_id"],
        "customer_name": state["customer"].get("name", ""),
        "message_text": event["message_text"],
        "event_type": event["event_type"],
        "contact": event["contact"],
        "contexto": contexto,
        "signals": signals,
        "decision_policy_excerpt": {
            "greetings": rules.get("greetings", []),
            "handoff": rules.get("handoff", {}),
            "scheduling": rules.get("scheduling", {}),
        },
        "kb_cliente_rules": client_data.get("rules", {}),
        "kb_cliente": client_data.get("kb", {}),
        "kb_active": state.get("client_profile", {}).get("kb_active", []),
        "identificacao": state.get("identificacao", {}),
    }

    return f"""Você interpreta mensagens para um workflow DigiSAC e retorna SOMENTE JSON válido.

Instruções de comportamento (bot_rules):
{bot_rules}

Restrições operacionais:
- Não afirme execução de ações.
- Inicie mensagem com [BOT].
- Preserve o contrato JSON já definido.
- Você é responsável por avaliar repetição SEMÂNTICA e mudança de trilha.
- Se a pergunta anterior já foi respondida, não repita a mesma intenção com outras palavras.
- Use identificação progressiva: não peça novamente dados já coletados.
- Retorne também um bloco \"analysis\" com:
  - same_diagnostic_path (bool)
  - user_answered_previous (bool)
  - should_change_path (bool)

Contexto:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()


def extract_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty response")
    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
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
    analysis = obj.get("analysis")
    if analysis is None or not isinstance(analysis, dict):
        obj["analysis"] = {
            "same_diagnostic_path": False,
            "user_answered_previous": False,
            "should_change_path": False,
        }
    else:
        obj["analysis"] = {
            "same_diagnostic_path": bool(analysis.get("same_diagnostic_path", False)),
            "user_answered_previous": bool(analysis.get("user_answered_previous", False)),
            "should_change_path": bool(analysis.get("should_change_path", False)),
        }
    return obj


def build_contexto_atualizado(state: Dict[str, Any], event_type: str) -> Dict[str, Any]:
    identificado = state.get("identificacao", {}).get("estado") == "concluida"
    return {
        "protocolo": state["protocol"],
        "numero": state["customer"].get("id", ""),
        "nome": state["customer"].get("name", ""),
        "empresa": state["customer"].get("company", ""),
        "identificado": identificado,
        "triagem_concluida": bool(state["workflow"].get("triagem_concluida", False)),
        "tentativas": int(state["workflow"].get("tentativas", 0)),
        "ticket_aberto": bool(state["ticket"].get("aberto", False)),
        "agendamento_enviado": bool(state["agendamento"].get("enviado", False)),
        "encerrado": bool(state["workflow"].get("encerrado", False)),
        "updated_at": state.get("updated_at", now_iso()),
        "last_status": state["workflow"].get("status", "INICIO"),
        "last_intent": state["workflow"].get("intent", ""),
        "last_event_type": event_type,
        "ultima_solicitacao_tipo": state["workflow"].get("ultima_solicitacao_tipo", ""),
        "ultima_pergunta_bot": state["conversation"].get("ultima_pergunta_bot", ""),
        "ultima_resposta_cliente": state["conversation"].get("ultima_resposta_cliente", ""),
        "historico_curto": state["conversation"].get("historico_curto", []),
        "resumo_turno": {
            "last_status": state["workflow"].get("status", "INICIO"),
            "last_intent": state["workflow"].get("intent", ""),
            "ultima_solicitacao_tipo": state["workflow"].get("ultima_solicitacao_tipo", ""),
            "tentativas": int(state["workflow"].get("tentativas", 0)),
            "tentativas_mesma_intencao": int(state["workflow"].get("tentativas_mesma_intencao", 0)),
            "coletas_repetidas": int(state["workflow"].get("coletas_repetidas", 0)),
        },
        "tentativas_falhas": state["workflow"].get("tentativas_falhas", []),
        "feedback_aprendizado": state["workflow"].get("feedback", {}),
        "identificacao_estado": state.get("identificacao", {}).get("estado", "nao_iniciada"),
        "identificacao_confianca": state.get("identificacao", {}).get("confianca", 0.0),
        "last_bot_message": normalize_text(state["conversation"].get("last_bot_message", "")),
    }


def hard_rules_decision(state: Dict[str, Any], event: Dict[str, Any], signals: Dict[str, Any], rules: Dict[str, Any], client_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event["is_automatic"]:
        return {
            "status": state["workflow"].get("status", "INICIO"),
            "intent": "ignorar_mensagem_automatica",
            "mensagem": "[BOT]",
            "confidence": 1.0,
            "abrir_ticket": False,
            "agendar": False,
            "handoff_humano": False,
            "motivo": "automatic_message_ignored",
        }

    if signals["closed_event"]:
        return {
            "status": "ENCERRADO",
            "intent": "closed_cleanup",
            "mensagem": "[BOT] Atendimento finalizado.",
            "confidence": 0.99,
            "abrir_ticket": False,
            "agendar": False,
            "handoff_humano": False,
            "motivo": "closed_event",
        }

    if signals["pedido_humano_explicito"]:
        return {
            "status": "FILA_N1",
            "intent": "handoff_humano",
            "mensagem": str((rules.get("handoff", {}) or {}).get("message_frustrated", "[BOT] Entendo 🙂 Vou transferir você para um atendente humano agora.")),
            "confidence": 0.99,
            "abrir_ticket": True,
            "agendar": False,
            "handoff_humano": True,
            "motivo": "pedido_humano_explicito",
        }

    if signals["only_greeting"] and state["workflow"].get("tentativas", 0) == 0:
        return {
            "status": "INICIO",
            "intent": "saudacao_inicial",
            "mensagem": rules.get("greeting_message", "[BOT] Olá 🙂 Posso te ajudar por aqui. Me conta o que você precisa."),
            "confidence": 0.98,
            "abrir_ticket": False,
            "agendar": False,
            "handoff_humano": False,
            "motivo": "greeting",
        }

    if signals["ambiguous_contact"]:
        return {
            "status": "IDENTIFICACAO",
            "intent": "identificacao",
            "mensagem": "[BOT] Para seguir com segurança, me confirme seu nome completo e empresa 🙂",
            "confidence": 0.98,
            "abrir_ticket": False,
            "agendar": False,
            "handoff_humano": False,
            "motivo": "ambiguous_contact",
        }

    if signals["pediu_tecnico_especifico"]:
        frustrated = signals["cliente_irritado"] or signals["bloqueio_real"]
        allow_scheduling = bool((rules.get("scheduling", {}) or {}).get("allow_on_soft_tech_request", True)) and bool((client_data.get("rules", {}) or {}).get("allow_scheduling", True))
        if frustrated:
            return {
                "status": "FILA_N1",
                "intent": "handoff_humano",
                "mensagem": str((rules.get("handoff", {}) or {}).get("message_frustrated", "[BOT] Entendo a sua frustração 🙂 Vou acionar um atendente para continuar com você.")),
                "confidence": 0.99,
                "abrir_ticket": True,
                "agendar": False,
                "handoff_humano": True,
                "motivo": "tech_request_with_frustration",
            }
        return {
            "status": "AGENDAMENTO" if allow_scheduling else "TRIAGEM",
            "intent": "preferencia_tecnico",
            "mensagem": str((rules.get("handoff", {}) or {}).get("message_soft_transition", "[BOT] Claro 🙂 Posso seguir por aqui ou já deixar o agendamento preparado.")),
            "confidence": 0.95,
            "abrir_ticket": False,
            "agendar": allow_scheduling,
            "handoff_humano": False,
            "motivo": "tech_request_soft",
        }

    return None


def call_openclaw(state: Dict[str, Any], event: Dict[str, Any], signals: Dict[str, Any], rules: Dict[str, Any], bot_rules: str, client_data: Dict[str, Any]) -> Dict[str, Any]:
    prompt = build_prompt(state, event, signals, rules, bot_rules, client_data)
    session_id = f"digisac-{event['protocol']}"
    cmd = [OPENCLAW_BIN, "agent", "--agent", OPENCLAW_AGENT, "--session-id", session_id, "--message", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=OPENCLAW_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"openclaw exited with {result.returncode}")
    ai = validate_ai_response(extract_json(result.stdout))
    ai["_raw_excerpt"] = (result.stdout or "")[:220]
    return ai


def build_contextual_fallback(state: Dict[str, Any]) -> Dict[str, Any]:
    last_intent = state.get("workflow", {}).get("intent", "")
    last_question = state.get("conversation", {}).get("ultima_pergunta_bot", "")
    if "impressora" in normalize_text(state.get("conversation", {}).get("last_user_message", "")):
        msg = "[BOT] Para avançar rápido: sua impressora está por USB ou rede/Wi‑Fi?"
    elif "identificacao" in normalize_text(last_intent):
        msg = "[BOT] Para continuar seu atendimento, me confirme seu nome completo e empresa."
    elif last_question:
        msg = "[BOT] Entendi. Sobre sua última resposta, consegue detalhar um pouco mais esse ponto?"
    else:
        msg = "[BOT] Entendi 🙂 Para avançar, você consegue me dizer quando esse problema começou?"
    return {
        "status": state.get("workflow", {}).get("status", "TRIAGEM") or "TRIAGEM",
        "intent": "fallback_local",
        "mensagem": msg,
        "confidence": 0.55,
        "abrir_ticket": False,
        "agendar": False,
        "handoff_humano": False,
        "actions_taken": ["fallback_response_contextual"],
        "analysis": {
            "same_diagnostic_path": False,
            "user_answered_previous": False,
            "should_change_path": True,
        },
    }


def apply_guardrails_to_decision(state: Dict[str, Any], raw: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    status = raw.get("status") or raw.get("status_sugerido") or "TRIAGEM"
    intent = raw.get("intent", "fallback_local")
    mensagem = raw.get("mensagem", "")
    confidence = float(raw.get("confidence", 0.5))
    handoff = bool(raw.get("handoff_humano") if "handoff_humano" in raw else raw.get("recomenda_handoff_humano", False))
    abrir_ticket = bool(raw.get("abrir_ticket") if "abrir_ticket" in raw else raw.get("recomenda_abrir_ticket", False))
    agendar = bool(raw.get("agendar") if "agendar" in raw else raw.get("recomenda_agendamento", False))
    analysis = raw.get("analysis", {}) or {}
    should_change_path = bool(analysis.get("should_change_path", False))
    same_diagnostic_path = bool(analysis.get("same_diagnostic_path", False))
    user_answered_previous = bool(analysis.get("user_answered_previous", False))

    actions_taken: List[str] = []

    # idempotência operacional de handoff/ticket
    if state["handoff"].get("realizado"):
        handoff = False
        abrir_ticket = False
        actions_taken.append("handoff_skipped_already_done")

    if handoff:
        status = "FILA_N1"
        intent = "handoff_humano"
        if not state["ticket"].get("aberto"):
            abrir_ticket = bool((rules.get("handoff", {}) or {}).get("open_ticket_when_handoff", True))
            if abrir_ticket:
                actions_taken.append("ticket_open_required")
        else:
            abrir_ticket = False
            actions_taken.append("ticket_reused_existing")
        agendar = False

    if status == "AGENDAMENTO":
        agendar = True
        handoff = False

    # Degradação progressiva por nível de repetição/travamento (configurável).
    same_intent = int(state["workflow"].get("tentativas_mesma_intencao", 0))
    repeated_collect = int(state["workflow"].get("coletas_repetidas", 0))
    weights = (rules.get("repeat_weights", {}) or {})
    thresholds = (rules.get("repeat_thresholds", {}) or {})
    w_should = int(weights.get("should_change_path", 1))
    w_same_diag = int(weights.get("same_diagnostic_path", 1))
    w_user_answered = int(weights.get("user_answered_previous", 1))
    w_same_high = int(weights.get("same_intent_high", 2))
    w_same_med = int(weights.get("same_intent_medium", 1))
    w_repeated_collect = int(weights.get("repeated_collect", 1))
    thr_light = int(thresholds.get("light", 1))
    thr_medium = int(thresholds.get("medium", 2))
    thr_high = int(thresholds.get("high", 4))

    repeat_score = 0
    repeat_score += w_should if should_change_path else 0
    repeat_score += w_same_diag if same_diagnostic_path else 0
    repeat_score += w_user_answered if user_answered_previous else 0
    repeat_score += w_same_high if same_intent >= 3 else (w_same_med if same_intent >= 2 else 0)
    repeat_score += w_repeated_collect if repeated_collect >= 2 else 0
    if bool(state.get("last_signals", {}).get("cliente_irritado", False)):
        repeat_score += 1
    if bool(state.get("last_signals", {}).get("bloqueio_real", False)):
        repeat_score += 1
    if int(state["workflow"].get("feedback", {}).get("falha", 0)) > int(state["workflow"].get("feedback", {}).get("sucesso", 0)):
        repeat_score += 1

    base_message = (mensagem or "").strip()
    if not base_message:
        base_message = "[BOT] Entendi."

    # Ajuste fino usando policy estruturada do kb_active do cliente.
    kb_policy = extract_kb_policy(state)
    escalation_mode = normalize_text(str(kb_policy.get("escalation_mode", "")))
    triage_depth = normalize_text(str(kb_policy.get("triage_depth", "")))
    if escalation_mode == "aggressive":
        thr_high = max(2, thr_high - 1)
    if escalation_mode == "soft":
        thr_medium += 1
    if triage_depth == "low":
        thr_medium = min(thr_medium, max(1, thr_light + 1))

    user_text = normalize_text(state.get("conversation", {}).get("last_user_message", ""))
    if "impressora" in user_text:
        break_suffix = " Para destravar, vamos por outro caminho: ela está conectada por USB ou rede/Wi‑Fi?"
    elif any(x in user_text for x in ["internet", "rede", "wifi", "wi-fi"]):
        break_suffix = " Para destravar, vamos por outro caminho: isso ocorre em todos os dispositivos ou só em um?"
    elif any(x in user_text for x in ["sistema", "acesso", "login"]):
        break_suffix = " Para destravar, vamos por outro caminho: o problema acontece para todos os usuários ou só para você?"
    else:
        break_suffix = " Para destravar, vou mudar o caminho: isso impacta sua operação total ou parcialmente?"

    # Prioridade explícita: alto -> médio -> leve
    failed_attempts = state["workflow"].get("tentativas_falhas", [])
    if any(isinstance(x, dict) and x.get("intent") == intent for x in failed_attempts[-5:]):
        repeat_score = max(repeat_score, thr_medium)
        actions_taken.append("failed_intent_detected")
    if any(isinstance(x, dict) and x.get("request_type") == state["workflow"].get("ultima_solicitacao_tipo", "") for x in failed_attempts[-5:]) and repeated_collect >= 1:
        repeat_score = max(repeat_score, thr_medium)
        actions_taken.append("repeated_request_type_block")

    if repeat_score >= thr_high and not handoff:
        status = "FILA_N1"
        intent = "handoff_por_repeticao"
        handoff = True
        abrir_ticket = bool((rules.get("handoff", {}) or {}).get("open_ticket_when_handoff", True))
        agendar = False
        mensagem = f"{base_message} Vou te encaminhar para um especialista humano para acelerar a solução."
        actions_taken.append("escalation_high_repetition")
    elif repeat_score >= thr_medium and not handoff:
        status = "TRIAGEM"
        intent = "quebra_repeticao_operacional"
        mensagem = f"{base_message}{break_suffix}"
        actions_taken.append("operational_path_break")
    elif (repeat_score >= thr_light) and not handoff:
        status = "TRIAGEM"
        intent = "mudanca_trilha_semantica"
        mensagem = f"{base_message} Vamos ajustar a abordagem para avançar melhor."
        actions_taken.append("semantic_path_change_applied")

    # Evita ficar preso no subfluxo de identificação indefinidamente.
    if status == "IDENTIFICACAO":
        ident = state.get("identificacao", {}) or {}
        if int(ident.get("tentativas", 0)) >= 2 and int(state["workflow"].get("respostas_uteis_cliente", 0)) >= 2:
            status = "TRIAGEM"
            intent = "identificacao_parcial_avancar"
            mensagem = "[BOT] Vamos avançar com o atendimento e completar seus dados em seguida, tudo bem?"
            actions_taken.append("identification_exit_guard")

    if status not in ALLOWED_STATUS:
        status = "TRIAGEM"
        actions_taken.append("invalid_status_normalized")

    if state["workflow"].get("encerrado") and status != "ENCERRADO":
        status = "ENCERRADO"
        intent = "closed_already"
        mensagem = "[BOT] Este atendimento já foi encerrado."
        handoff = False
        agendar = False
        abrir_ticket = False
        actions_taken.append("locked_closed_state")

    return {
        "status": status,
        "intent": intent,
        "mensagem": mensagem,
        "confidence": confidence,
        "abrir_ticket": abrir_ticket,
        "agendar": agendar,
        "handoff_humano": handoff,
        "actions_taken": actions_taken,
        "raw_output_excerpt": raw.get("_raw_excerpt", ""),
        "analysis": analysis,
    }


def persist_decision(state: Dict[str, Any], event: Dict[str, Any], decision: Dict[str, Any], rules: Dict[str, Any], signals: Dict[str, Any], source: str, fallback_used: bool) -> Dict[str, Any]:
    is_automatic_ignored = decision["intent"] == "ignorar_mensagem_automatica"
    if is_automatic_ignored:
        msg = ""
    else:
        msg = apply_human_format(anti_loop_guard(state, decision["mensagem"], rules, signals), rules)

    prev_intent = state["workflow"].get("intent", "")
    prev_request_type = state["workflow"].get("ultima_solicitacao_tipo", "")
    state["workflow"]["status"] = decision["status"]
    state["workflow"]["intent"] = decision["intent"]
    should_increment_attempt = bool(event.get("customer_turn_recorded")) and bool(event.get("customer_turn_useful"))
    if should_increment_attempt:
        state["workflow"]["tentativas"] = int(state["workflow"].get("tentativas", 0)) + 1
        state["workflow"]["respostas_uteis_cliente"] = int(state["workflow"].get("respostas_uteis_cliente", 0)) + 1
    if prev_intent and prev_intent == decision["intent"]:
        state["workflow"]["tentativas_mesma_intencao"] = int(state["workflow"].get("tentativas_mesma_intencao", 0)) + 1
    state["workflow"]["encerrado"] = bool(state["workflow"].get("encerrado") or decision["status"] == "ENCERRADO")
    state["workflow"]["ultima_solicitacao_tipo"] = infer_request_type(decision["status"], msg, state["workflow"].get("ultima_solicitacao_tipo", ""))
    if prev_request_type and prev_request_type == state["workflow"]["ultima_solicitacao_tipo"]:
        state["workflow"]["coletas_repetidas"] = int(state["workflow"].get("coletas_repetidas", 0)) + 1
    if bool(decision.get("analysis", {}).get("should_change_path", False)):
        state["workflow"]["tentativas_mesma_intencao"] = 0
        state["workflow"]["coletas_repetidas"] = 0
    if any(a in decision.get("actions_taken", []) for a in ["operational_path_break", "escalation_high_repetition"]):
        failed = state["workflow"].get("tentativas_falhas", [])
        failed.append({
            "intent": prev_intent or decision["intent"],
            "request_type": prev_request_type or state["workflow"].get("ultima_solicitacao_tipo", ""),
            "at": now_iso(),
            "reason": ",".join(decision.get("actions_taken", [])),
        })
        state["workflow"]["tentativas_falhas"] = failed[-20:]

    ident = state.get("identificacao", {})
    ident["nome_coletado"] = bool(state["customer"].get("name"))
    ident["empresa_coletada"] = bool(state["customer"].get("company"))
    ident["tentativas"] = int(ident.get("tentativas", 0)) + (1 if decision["status"] == "IDENTIFICACAO" else 0)
    if decision["status"] != "IDENTIFICACAO" and ident.get("nome_coletado") and ident.get("empresa_coletada"):
        ident["estado"] = "concluida"
        ident["confianca"] = max(float(ident.get("confianca", 0.0)), 0.8)
    elif decision["status"] == "IDENTIFICACAO" and (ident.get("nome_coletado") or ident.get("empresa_coletada")):
        ident["estado"] = "parcial"
        ident["confianca"] = max(float(ident.get("confianca", 0.0)), 0.5)
    elif decision["status"] == "IDENTIFICACAO":
        ident["estado"] = "nao_iniciada"
    state["identificacao"] = ident

    if decision["abrir_ticket"] and not state["ticket"].get("aberto"):
        state["ticket"]["aberto"] = True
        state["ticket"]["opened_at"] = now_iso()

    if decision["handoff_humano"] and not state["handoff"].get("realizado"):
        state["handoff"]["realizado"] = True
        state["handoff"]["motivo"] = decision["intent"]
        state["handoff"]["timestamp"] = now_iso()
        state["handoff"]["bot_silenciado"] = True

    if decision["agendar"]:
        state["agendamento"]["enviado"] = True
        state["agendamento"]["timestamp"] = now_iso()

    if decision["status"] == "ENCERRADO":
        state["is_open"] = False

    if msg and msg != "[BOT]":
        state["conversation"]["last_bot_message"] = msg
        if "?" in msg:
            state["conversation"]["ultima_pergunta_bot"] = msg
        append_history(state, "assistant", msg, now_iso(), "bridge")

    state["updated_at"] = now_iso()

    output = {
        "decisao": {
            "status": decision["status"],
            "intent": decision["intent"],
            "mensagem": msg,
            "confidence": decision["confidence"],
            "abrir_ticket": decision["abrir_ticket"],
            "agendar": decision["agendar"],
            "handoff_humano": decision["handoff_humano"],
        },
        "contexto_atualizado": build_contexto_atualizado(state, event["event_type"]),
        "meta": {
            "source": source,
            "fallback_used": fallback_used,
            "state_persisted": True,
            "state_version": STATE_VERSION,
            "raw_output_excerpt": decision.get("raw_output_excerpt", "")[:220],
            "analysis": decision.get("analysis", {}),
        },
    }

    state["last_execution"] = {
        "input_payload": event["raw_payload"],
        "decision_output": output,
        "actions_taken": decision.get("actions_taken", []),
        "event_id": event["event_id"],
    }

    save_protocol_state(event["protocol"], state)
    if decision["status"] == "ENCERRADO":
        archive_protocol_state(event["protocol"])
    return output


def run_decision(req: DecisionRequest) -> Dict[str, Any]:
    with protocol_lock(req.protocol):
        rules = load_decision_rules()
        bot_rules = load_bot_rules()
        client_data = load_client_data(req)

        state = load_protocol_state(req)
        event = normalize_event(req)

        # idempotência por evento
        if state.get("last_execution", {}).get("event_id") == event["event_id"] and state.get("last_execution", {}).get("decision_output"):
            replay = copy.deepcopy(state["last_execution"]["decision_output"])
            replay["meta"]["source"] = "idempotent_replay"
            return replay

        state = consolidate_state(state, event)
        signals = detect_signals(state, event, rules)
        state["last_signals"] = signals

        if state.get("handoff", {}).get("realizado") and state.get("handoff", {}).get("bot_silenciado", False):
            handoff_live = {
                "status": "FILA_N1",
                "intent": "handoff_em_andamento",
                "mensagem": "",
                "confidence": 1.0,
                "abrir_ticket": False,
                "agendar": False,
                "handoff_humano": False,
                "actions_taken": ["bot_silenced_after_handoff"],
            }
            final = apply_guardrails_to_decision(state, handoff_live, rules)
            return persist_decision(state, event, final, rules, signals, source="handoff_lock", fallback_used=False)

        hard = hard_rules_decision(state, event, signals, rules, client_data)
        if hard:
            final = apply_guardrails_to_decision(state, hard, rules)
            return persist_decision(state, event, final, rules, signals, source="hard_rules", fallback_used=False)

        # caminho único da IA -> guardrails -> persistência
        try:
            ai = call_openclaw(state, event, signals, rules, bot_rules, client_data)
        except Exception:
            ai = build_contextual_fallback(state)
            final = apply_guardrails_to_decision(state, ai, rules)
            return persist_decision(state, event, final, rules, signals, source="bridge_fallback_v3", fallback_used=True)

        final = apply_guardrails_to_decision(state, ai, rules)
        return persist_decision(state, event, final, rules, signals, source="openclaw_cli_v2_1", fallback_used=False)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "openclaw-decision-bridge",
        "mode": "openclaw-cli-v2_1",
        "state_version": STATE_VERSION,
    }


@app.post("/decision/openclaw")
def decision_openclaw(payload: DecisionRequest, authorization: Optional[str] = Header(default="")) -> Dict[str, Any]:
    if BRIDGE_TOKEN and authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")
    return run_decision(payload)
