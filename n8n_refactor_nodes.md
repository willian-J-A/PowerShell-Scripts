# Ajustes de workflow n8n (sem mudar contrato da API)

## Objetivo
Usar o backend FastAPI como **fonte única de decisão** e transformar o n8n em orquestrador por flags:
- `handoff_humano`
- `abrir_ticket`
- `agendar`

Além disso, persistir e reaproveitar `contexto_atualizado` para garantir continuidade entre mensagens.

## 1) Nodes para remover da decisão conversacional duplicada
Remova da rota principal os nodes que tomam decisão por `STATUS` localmente:
- `Switch - Route by STATUS`
- `Code - Resolver Acao Conversacional`
- `Set - Msg STATUS INICIO`
- `Set - Msg STATUS AGUARDANDO_IDENTIFICACAO`
- `Set - Msg STATUS AGUARDANDO_VALIDACAO`
- `Set - Msg Retry`
- `Set - Msg Encerrado`

Esses nodes duplicam regra já decidida no backend e causam inconsistência.

## 2) Persistência de estado e contexto (obrigatório)
### 2.1 Carregar estado local antes de chamar o bridge
No `Code - Load JSON State V2` (ou equivalente), carregar estado por chave de correlação (`protocol::customer_id`) com fallback de contexto vazio.

### 2.2 Salvar `contexto_atualizado` após resposta
Após `Set - Normalizar Resposta OpenClaw`, atualizar o estado local:
- `state.contexto = $json.contexto_atualizado`
- `state.ticket_id` (quando existir)
- `state.updated_at`

Exemplo de trecho para node Code:
```js
const fs = require('fs');
const path = '/tmp/n8n_kb_estado.json';
let db = { kb: {}, estado: {} };
if (fs.existsSync(path)) {
  try { db = JSON.parse(fs.readFileSync(path, 'utf8')); } catch (_) {}
}

const key = `${$('Set - Normalizar Payload').item.json.protocol}::${$('Set - Normalizar Payload').item.json.customer_id}`;
const prev = db.estado[key] || {};
const ctx = $json.contexto_atualizado || prev.contexto || {};

db.estado[key] = {
  ...prev,
  protocolo: $('Set - Normalizar Payload').item.json.protocol,
  numero: $('Set - Normalizar Payload').item.json.customer_id,
  ticket_id: prev.ticket_id || '',
  contexto: ctx,
  updated_at: new Date().toISOString(),
};

fs.writeFileSync(path, JSON.stringify(db, null, 2));
return [{ json: { ...$json, state: db.estado[key] } }];
```

## 3) Montagem correta de `contexto` antes do `/decision/openclaw`
No payload da chamada ao bridge, montar `contexto` usando o estado local mais recente (`state.contexto`) e fallback para valores default.

Campos obrigatórios:
- `protocolo`
- `numero`
- `nome`
- `empresa`
- `identificado`
- `triagem_concluida`
- `tentativas`
- `ticket_aberto`
- `agendamento_enviado`
- `encerrado`
- `last_status`
- `last_intent`
- `last_event_type`
- `ultima_solicitacao_tipo`

## 4) Payload compatível com FastAPI (exato)
No `HTTP Request - OpenClaw Fallback`, enviar exatamente:
```json
{
  "protocol": "...",
  "customer_id": "...",
  "customer_name": "...",
  "message_text": "...",
  "event_type": "...",
  "contact": {
    "contactId": "...",
    "serviceId": "...",
    "ambiguous_contact": false,
    "contact_count": 0
  },
  "contexto": { ... }
}
```

E passar token por variável de ambiente:
- Header `Authorization: Bearer {{$env.OPENCLAW_BRIDGE_TOKEN}}`

## 5) Node de normalização de resposta
No `Set - Normalizar Resposta OpenClaw`, mapear da resposta do bridge:
- `status = {{$json.decisao.status}}`
- `intent = {{$json.decisao.intent}}`
- `mensagem = {{$json.decisao.mensagem}}`
- `handoff_humano = {{$json.decisao.handoff_humano}}`
- `abrir_ticket = {{$json.decisao.abrir_ticket}}`
- `agendar = {{$json.decisao.agendar}}`
- `contexto_atualizado = {{$json.contexto_atualizado}}`

## 6) Roteamento único por flags + status
Trocar `Switch - Rota OpenClaw` por branches simples na ordem:
1. IF `status == "ENCERRADO"` -> branch encerramento
2. IF `handoff_humano == true` -> branch handoff
3. IF `agendar == true` -> branch agendamento
4. default -> enviar `mensagem` ao cliente

## 7) Branch handoff (com verificação de ticket existente)
Ordem obrigatória:
1. IF `abrir_ticket == true`
2. IF ticket existente (`state.ticket_id` não vazio)
   - Se **já existe**: `HTTP Request - DigiSAC Comment` + `HTTP Request - Enviar Mensagem Generica DigiSAC`
   - Se **não existe**: `HTTP Request - Abrir Ticket` (ou transferir) -> salvar `ticket_id` no state -> `HTTP Request - DigiSAC Comment` -> `HTTP Request - Enviar Mensagem Generica DigiSAC`

> Regra: quando `handoff_humano=true`, o backend já deve vir com `abrir_ticket=true`. O n8n apenas executa.

## 8) Branch `status == ENCERRADO`
Adicionar tratamento explícito:
1. Atualizar `state.contexto.encerrado = true`
2. Persistir `last_status = ENCERRADO` e `last_intent`
3. Enviar `mensagem` do bridge ao cliente (quando aplicável)
4. Registrar comentário operacional opcional

## 9) Branch agendamento
- Reaproveite `HTTP Request - Enviar Link Bookings DigiSAC`.
- Envie comentário de log com `Set - Comment Agendamento`.
- Persistir `state.contexto.agendamento_enviado = true` quando envio ocorrer.

## 10) Simplificação de `quality_ok`
No `Code - Load JSON State V2`, usar regra simples e permissiva (sem bloquear mensagem curta útil):

```js
const src = String($('Set - Normalizar Payload').item.json.message_text || '').trim();
const low = src.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
const usefulShort = new Set(['oi', 'ola', 'olá', 'bom dia', 'boa tarde', 'boa noite', 'sim', 'ok', 'pode', '1', '2']);
const quality_ok = src.length >= 2 || usefulShort.has(low);
```

## 11) Remoção de token hardcoded
Nos nodes HTTP DigiSAC, trocar `Bearer 0e2b...` por:
- `Bearer {{$env.DIGISAC_API_TOKEN}}`

## Fluxo final esperado
Webhook -> Normalizar -> Carregar estado/contexto -> Chamar OpenClaw bridge -> Normalizar decisão -> Persistir `contexto_atualizado` ->
- `status=ENCERRADO` => atualizar estado + mensagem de encerramento
- `handoff_humano` => validar ticket existente / abrir ticket + comentar + mensagem
- `agendar` => enviar link bookings
- default => enviar mensagem
