from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
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
ALLOWED_STATUS = {
    "INICIO", "IDENTIFICACAO", "TRIAGEM", "BASE_CONHECIMENTO", "FILA_N1",
    "SOLUCAO_PROPOSTA", "AGUARDANDO_VALIDACAO", "AGENDAMENTO",
    "AGUARDANDO_RESPOSTA", "RESERVA_DATA", "ENCERRADO"
}

GREETING_MESSAGE = "[BOT] Olá 🙂 Posso te ajudar por aqui. Me conta o que você precisa."
SPECIFIC_TECH_HANDOFF_MESSAGE = "[BOT] Só um momento 🙂 vou verificar se ele está disponível."
IRRITATION_HANDOFF_MESSAGE = "[BOT] Entendo a sua frustração 🙂 Assim que um atendente estiver disponível, ele assumirá o atendimento."

TECH_NAMES = ["william", "willian", "breno", "erivelton"]
TECH_ALIASES = {
    "wiliam": "william",
    "wilian": "willian",
    "eriveton": "erivelton",
    "erivelto": "erivelton",
    "brenoo": "breno",
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def normalize_text(text: str) -> str:
    return ' '.join((text or '').strip().lower().split())


def strip_accents(text: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', text or '')
        if unicodedata.category(c) != 'Mn'
    )


def fuzzy_contains_tech_name(text: str) -> bool:
    base = strip_accents(text).lower()
    tokens = re.findall(r"[a-z]+", base)
    expanded = [TECH_ALIASES.get(t, t) for t in tokens]

    if any(name in base for name in TECH_NAMES):
        return True

    for token in expanded:
        for target in TECH_NAMES:
            if abs(len(token) - len(target)) <= 1 and levenshtein_distance(token, target) <= 1:
                return True
    return False


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
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            rep = prev[j - 1] + (ca != cb)
            curr.append(min(ins, dele, rep))
        prev = curr
    return prev[-1]


def detect_signals(req: DecisionRequest) -> Dict[str, Any]:
    text = normalize_text(req.message_text or '')
    text_no_accent = strip_accents(text)
    irritacao_terms = [
        'demorando', 'ninguem resolve', 'absurdo', 'ridiculo',
        'sacanagem', 'frustrado', 'frustrada', 'irritado', 'irritada',
        'irritante', 'pessimo', 'horrivel', 'faz tempo', 'ate agora nada',
        'bloqueado', 'bloqueada', 'sem acesso', 'nao consigo acessar'
    ]
    saudacoes = {'oi', 'ola', 'bom dia', 'boa tarde', 'boa noite'}

    first_message = not bool(req.contexto.last_status or req.contexto.last_intent or req.contexto.updated_at)
    identified = bool(req.contexto.identificado or (req.contexto.nome and req.contexto.empresa))
    only_greeting = text_no_accent in saudacoes
    short_useful = len(text.split()) <= 3 and not only_greeting

    return {
        'text': text,
        'first_message': first_message,
        'identified': identified,
        'pediu_tecnico_especifico': fuzzy_contains_tech_name(text),
        'cliente_irritado': any(term in text_no_accent for term in irritacao_terms),
        'only_greeting': only_greeting,
        'short_useful': short_useful,
        'ambiguous_contact': bool(req.contact.ambiguous_contact),
        'closed_event': req.event_type in {'closed', 'conversation_closed', 'finalized'},
        'ultima_solicitacao_tipo': req.contexto.ultima_solicitacao_tipo or '',
    }


def hard_rules_decision(req: DecisionRequest, signals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if signals['closed_event']:
        return build_decision(req, status='ENCERRADO', intent='closed_cleanup', mensagem='[BOT] Atendimento finalizado.', abrir_ticket=False, agendar=False, handoff_humano=False, source='hard_rules')

    # 1) Técnico específico -> handoff imediato + abrir ticket obrigatório
    if signals['pediu_tecnico_especifico']:
        return build_decision(req, status='FILA_N1', intent='handoff_humano', mensagem=SPECIFIC_TECH_HANDOFF_MESSAGE, abrir_ticket=True, agendar=False, handoff_humano=True, source='hard_rules')

    # 2) Irritação + bloqueio -> handoff
    if signals['cliente_irritado'] and ('bloque' in strip_accents(signals['text']) or 'sem acesso' in strip_accents(signals['text'])):
        return build_decision(req, status='FILA_N1', intent='handoff_humano', mensagem=IRRITATION_HANDOFF_MESSAGE, abrir_ticket=True, agendar=False, handoff_humano=True, source='hard_rules')

    # 3) Contato ambíguo -> identificação
    if signals['ambiguous_contact']:
        return build_decision(req, status='IDENTIFICACAO', intent='identificacao', mensagem='[BOT] Para seguir com segurança, me confirme seu nome completo e empresa 🙂', abrir_ticket=False, agendar=False, handoff_humano=False, source='hard_rules')

    # 4) Saudação curta -> resposta leve
    if signals['only_greeting']:
        return build_decision(req, status='INICIO', intent='saudacao_inicial', mensagem=GREETING_MESSAGE, abrir_ticket=False, agendar=False, handoff_humano=False, source='hard_rules')

    return None


def build_decision(req: DecisionRequest, *, status: str, intent: str, mensagem: str, abrir_ticket: bool, agendar: bool, handoff_humano: bool, source: str) -> Dict[str, Any]:
    updated = {
        **req.contexto.model_dump(),
        'protocolo': req.protocol,
        'numero': req.customer_id,
        'updated_at': now_iso(),
        'last_status': status,
        'last_intent': intent,
        'last_event_type': req.event_type,
        'ultima_solicitacao_tipo': infer_request_type(status, mensagem, req.contexto.ultima_solicitacao_tipo or ''),
        'ticket_aberto': bool(req.contexto.ticket_aberto or abrir_ticket),
        'agendamento_enviado': bool(req.contexto.agendamento_enviado or agendar),
    }
    if status == 'ENCERRADO':
        updated['encerrado'] = True

    return {
        'decisao': {
            'status': status,
            'intent': intent,
            'mensagem': mensagem,
            'confidence': 0.99 if source == 'hard_rules' else 0.55,
            'abrir_ticket': abrir_ticket,
            'agendar': agendar,
            'handoff_humano': handoff_humano,
        },
        'contexto_atualizado': updated,
        'meta': {'source': source, 'fallback_used': source != 'openclaw_cli_v2_2'},
    }


def infer_request_type(status: str, mensagem: str, previous: str) -> str:
    if status == 'IDENTIFICACAO':
        return 'identificacao'
    if status in {'TRIAGEM', 'BASE_CONHECIMENTO', 'SOLUCAO_PROPOSTA', 'AGUARDANDO_VALIDACAO'} and '?' in mensagem:
        return 'triagem'
    if status == 'AGENDAMENTO':
        return 'agendamento'
    if status == 'FILA_N1':
        return 'handoff'
    return previous


def fallback_response(req: DecisionRequest, signals: Optional[Dict[str, Any]] = None, intent: str = 'fallback_local') -> Dict[str, Any]:
    signals = signals or detect_signals(req)
    hard_decision = hard_rules_decision(req, signals)
    if hard_decision:
        return hard_decision

    # 5) Caso contrário -> triagem
    return build_decision(
        req,
        status='TRIAGEM',
        intent=intent,
        mensagem='[BOT] Perfeito 🙂 Me conta um pouco mais do que está acontecendo para eu te ajudar melhor.',
        abrir_ticket=False,
        agendar=False,
        handoff_humano=False,
        source='bridge_fallback_v2_2',
    )


def build_prompt(req: DecisionRequest, signals: Dict[str, Any]) -> str:
    payload = {
        'protocol': req.protocol,
        'customer_id': req.customer_id,
        'customer_name': req.customer_name,
        'message_text': req.message_text,
        'event_type': req.event_type,
        'contactId': req.contact.contactId,
        'serviceId': req.contact.serviceId,
        'ambiguous_contact': req.contact.ambiguous_contact,
        'contact_count': req.contact.contact_count,
        'contexto_temp': req.contexto.model_dump(),
        'signals': signals,
        'kb_active': [],
    }
    return f'''Você interpreta mensagens para um workflow DigiSAC.
Retorne SOMENTE JSON válido no formato esperado.

Regras:
- Nunca responda fora do JSON.
- Nunca diga que algo foi executado.
- Em todas as mensagens, inicie com [BOT].
- Se não houver regra crítica, siga triagem objetiva.

Contexto do atendimento:
{json.dumps(payload, ensure_ascii=False, indent=2)}
'''.strip()


def extract_json(text: str) -> Dict[str, Any]:
    text = (text or '').strip()
    if not text:
        raise ValueError('empty response')
    try:
        return json.loads(text)
    except Exception:
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def validate_ai_response(obj: Dict[str, Any]) -> Dict[str, Any]:
    required = ['status_sugerido', 'intent', 'mensagem', 'confidence', 'recomenda_abrir_ticket', 'recomenda_handoff_humano', 'recomenda_agendamento', 'motivo']
    if not all(k in obj for k in required):
        raise ValueError('missing required keys')
    if obj['status_sugerido'] not in ALLOWED_STATUS:
        raise ValueError('invalid status_sugerido')
    conf = float(obj['confidence'])
    if conf < 0 or conf > 1:
        raise ValueError('confidence out of range')
    if not isinstance(obj['recomenda_abrir_ticket'], bool) or not isinstance(obj['recomenda_handoff_humano'], bool) or not isinstance(obj['recomenda_agendamento'], bool):
        raise ValueError('invalid boolean fields')
    if not str(obj['mensagem']).startswith('[BOT]'):
        raise ValueError('mensagem must start with [BOT]')
    return obj


def map_to_bridge_response(req: DecisionRequest, ai: Dict[str, Any], signals: Dict[str, Any]) -> Dict[str, Any]:
    # hard rules always win
    hard_decision = hard_rules_decision(req, signals)
    if hard_decision:
        return hard_decision

    status = ai.get('status_sugerido') or 'TRIAGEM'
    intent = ai.get('intent') or 'fallback_local'
    mensagem = ai.get('mensagem', '')
    handoff = bool(ai.get('recomenda_handoff_humano', False))
    abrir_ticket = bool(ai.get('recomenda_abrir_ticket', False))
    agendar = bool(ai.get('recomenda_agendamento', False))

    # consistência operacional status/intent/flags
    if handoff:
        status = 'FILA_N1'
        intent = 'handoff_humano'
        abrir_ticket = True
        agendar = False

    if status == 'AGENDAMENTO':
        agendar = True
        handoff = False

    return {
        'decisao': {
            'status': status,
            'intent': intent,
            'mensagem': mensagem,
            'confidence': float(ai.get('confidence', 0.5)),
            'abrir_ticket': abrir_ticket,
            'agendar': agendar,
            'handoff_humano': handoff,
        },
        'contexto_atualizado': {
            **req.contexto.model_dump(),
            'protocolo': req.protocol,
            'numero': req.customer_id,
            'updated_at': now_iso(),
            'last_status': status,
            'last_intent': intent,
            'last_event_type': req.event_type,
            'ultima_solicitacao_tipo': infer_request_type(status, mensagem, req.contexto.ultima_solicitacao_tipo or ''),
            'ticket_aberto': bool(req.contexto.ticket_aberto or abrir_ticket),
            'agendamento_enviado': bool(req.contexto.agendamento_enviado or agendar),
            'encerrado': bool(req.contexto.encerrado or status == 'ENCERRADO' or intent == 'closed_cleanup'),
        },
        'meta': {
            'source': 'openclaw_cli_v2_2',
            'fallback_used': False,
            'motivo': ai.get('motivo', ''),
        }
    }


def call_openclaw(req: DecisionRequest) -> Dict[str, Any]:
    signals = detect_signals(req)

    hard_decision = hard_rules_decision(req, signals)
    if hard_decision:
        return hard_decision

    prompt = build_prompt(req, signals)
    session_id = f'digisac-{req.protocol}'
    cmd = [OPENCLAW_BIN, 'agent', '--agent', OPENCLAW_AGENT, '--session-id', session_id, '--message', prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=OPENCLAW_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f'openclaw exited with {result.returncode}')
    ai = validate_ai_response(extract_json(result.stdout))
    return map_to_bridge_response(req, ai, signals)


@app.get('/health')
def health():
    return {'ok': True, 'service': 'openclaw-decision-bridge', 'mode': 'openclaw-cli-v2_2'}


@app.post('/decision/openclaw')
def decision_openclaw(payload: DecisionRequest, authorization: Optional[str] = Header(default='')):
    if BRIDGE_TOKEN and authorization != f'Bearer {BRIDGE_TOKEN}':
        raise HTTPException(status_code=401, detail='unauthorized')
    signals = detect_signals(payload)

    hard_decision = hard_rules_decision(payload, signals)
    if hard_decision:
        return hard_decision

    try:
        return call_openclaw(payload)
    except Exception:
        return fallback_response(payload, signals=signals)
