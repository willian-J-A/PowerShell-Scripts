Leia este pedido inteiro antes de alterar qualquer arquivo.

Quero refatorar este sistema de atendimento para que ele fique previsível, modular, seguro e fácil de evoluir no dia a dia sem precisar reiniciar gateway/OpenClaw nem arriscar sobrescrever o core.

Trate isso como uma tarefa de engenharia de produção, não como experimento de prompt.

Arquivos principais envolvidos:
1. /openclaw_decision_bridge_v2_1.py
2. /wf_live_latest.json

Quero que você proponha e implemente uma estrutura com 4 camadas separadas:

1) CORE IMUTÁVEL
- arquivo Python principal do bridge
- responsável por:
  - validação de payload
  - leitura de contexto
  - aplicação de guardrails operacionais mínimos
  - execução da IA
  - pós-processamento
  - retorno da decisão final
- este arquivo NÃO deve ser editável dinamicamente pela IA
- este arquivo NÃO deve depender de restart para refletir mudanças de regras/KB externas
- este arquivo deve permanecer estável e previsível

2) PROMPT / REGRAS CONVERSACIONAIS DO BOT
- arquivo externo separado do core
- ex.: /config/bot_rules.md ou /config/bot_rules.json
- contém:
  - tom
  - estilo
  - formatação
  - o que fazer e o que não fazer na conversa
  - exemplos
  - política de emojis
  - política de listas, quebras de linha e naturalidade
- NÃO contém regra operacional crítica
- deve ser recarregado dinamicamente sem restart

3) REGRAS OPERACIONAIS
- arquivo externo separado do core
- ex.: /config/decision_rules.json
- contém:
  - nomes de técnicos
  - regras de handoff
  - regras de ticket
  - regras de agendamento
  - regras de encerramento
  - palavras de frustração
  - saudações
  - política de transição suave
  - thresholds
  - comportamento por canal se necessário
- deve ser recarregado dinamicamente sem restart
- o OpenClaw pode consultar essas regras, mas não pode sobrescrever o core

4) KB POR CLIENTE
- estrutura por cliente
- ex.: /kb/clientes/<cliente>/rules.json e /kb/clientes/<cliente>/kb.json
- contém:
  - escopo de atendimento
  - restrições
  - exclusões
  - responsáveis
  - políticas específicas
  - perguntas e respostas conhecidas
- deve ser recarregado dinamicamente sem restart
- precisa permitir atualização frequente sem tocar no código Python principal

IMPORTANTE:
OpenClaw / IA NÃO pode sobrescrever a programação do bridge.
Mudanças do dia a dia devem acontecer apenas em:
- bot_rules
- decision_rules
- KB dos clientes

==================================================
PROBLEMAS REAIS QUE PRECISAM SER CORRIGIDOS
==================================================

Hoje o sistema apresenta vários problemas práticos:

1. REPETIÇÃO E LOOP
- o bot repete perguntas genéricas como:
  "Perfeito 🙂 Me conta um pouco mais do que está acontecendo..."
- repete isso mesmo quando o cliente já informou algo útil como:
  - "Ela está desligando"
  - "A impressora não tá funcionando"
  - "A empresa é cyber bridge"
- isso gera sensação de bot burro e destrói a experiência

2. FALTA DE PROGRESSÃO
- o bot não evolui adequadamente entre:
  - saudação
  - identificação
  - triagem
  - diagnóstico inicial
  - ação
- ele parece ficar preso em fallback genérico

3. SAÍDA SECA OU FORMAL DEMAIS
- respostas tecnicamente corretas, mas frias
- texto com cara de documentação/manual
- pouco natural para WhatsApp
- falta quebra de linha, ritmo e variação
- uso de emoji inconsistente
- preciso de humanização controlada, não exagerada

4. DUPLICIDADE DE LÓGICA
- há lógica conversacional em mais de um lugar
- o n8n não pode continuar decidindo conversa localmente se o backend já devolve a decisão
- o n8n deve ser orquestrador por flags e não segundo cérebro do atendimento

5. CONTEXTO / MEMÓRIA OPERACIONAL
- contexto precisa ser persistido e reaproveitado entre mensagens
- contexto_atualizado precisa voltar ao estado local e ser usado na próxima chamada
- o sistema não pode agir como se cada mensagem fosse isolada

6. HANDOFF / TRANSIÇÃO SUAVE
- menção a técnico específico NÃO deve virar handoff imediato por padrão
- quero transição suave
- se o cliente citar um técnico específico sem frustração:
  - tratar como preferência
  - responder com suavidade
  - permitir continuidade por ali
  - permitir agendamento se fizer sentido
- se houver frustração, insistência ou bloqueio real:
  - aí sim permitir handoff humano + ticket
- não quero um bot engessado nem um bot que terceiriza tudo

7. AGENDAMENTO
- se não houver frustração e a política permitir, pode sugerir ou acionar agendamento como próximo passo
- isso deve ser governado por rules.json e/ou KB do cliente

8. TICKET
- nunca duplicar ticket
- handoff operacional real deve abrir ticket se não existir
- ticket existente deve ser reaproveitado

9. HOT RELOAD
- mudança em decision_rules.json deve refletir sem restart
- mudança em bot_rules deve refletir sem restart
- mudança em KB deve refletir sem restart
- idealmente usar cache com verificação por mtime
- não quero reiniciar gateway/OpenClaw para cada ajuste pequeno

10. SEGURANÇA DE ESTRUTURA
- IA não pode editar o core Python
- IA não pode transformar regra operacional em comportamento implícito escondido
- guardrails críticos têm que ficar no backend

==================================================
DIVISÃO DE RESPONSABILIDADE ESPERADA
==================================================

Backend / core determinístico:
- validação
- leitura de estado/contexto
- hot reload de arquivos externos
- prevenção de duplicidade de ticket
- regras mínimas de proteção
- encerramento
- aplicação de transição suave vs handoff real
- controle anti-loop
- formatação final da mensagem para manter padrão mínimo
- impedir que a IA sobrescreva o core

IA / OpenClaw:
- interpretar intenção
- conduzir triagem
- adaptar linguagem
- entender mudança de contexto
- sugerir próxima etapa
- gerar resposta natural
- mas sem ser a autoridade final em regra operacional crítica

n8n:
- carregar estado/contexto local
- chamar o bridge
- persistir contexto_atualizado
- rotear por flags
- abrir/transferir ticket
- agendar
- enviar mensagem
- comentar operacionalmente
- NÃO decidir conversa por conta própria

==================================================
COMPORTAMENTO OBRIGATÓRIO
==================================================

1. Saudação curta
Mensagens:
- "oi"
- "olá"
- "bom dia"
- "boa tarde"
- "boa noite"

Comportamento:
- resposta leve
- sem pedir identificação imediatamente
- sem handoff imediato
- sem triagem agressiva

Exemplo aceitável:
"[BOT] Olá 🙂 Posso te ajudar por aqui. Me conta o que você precisa."

2. Menção a técnico específico sem frustração
Exemplo:
- "fala com william"
- "quero falar com o Breno"

Comportamento:
- NÃO fazer handoff imediato por padrão
- interpretar como preferência
- fazer transição suave
- se a política permitir, pode oferecer continuidade por ali ou agendamento
- handoff_humano = false inicialmente
- abrir_ticket = false inicialmente
- agendar = true se fizer sentido pela regra

Exemplo aceitável:
"[BOT] Claro 🙂 Vou verificar a disponibilidade dele. Se preferir, já posso te orientar por aqui ou seguir com o agendamento."

3. Menção a técnico específico com frustração
Exemplo:
- "ninguém resolve, quero falar com o william agora"
- "já faz tempo, me passa pro Breno"

Comportamento:
- handoff_humano = true
- abrir_ticket = true
- agendar = false
- transição clara e humana
- sem continuar triagem

4. Diagnóstico com informação suficiente
Exemplo:
- "A impressora não tá funcionando"
- "Ela está desligando"
- "Ela desliga depois de 30 minutos"

Comportamento:
- NÃO repetir "me conta mais" infinitamente
- avançar para diagnóstico inicial
- fazer pergunta útil e específica
- não repetir a mesma solicitação em mensagens consecutivas

5. Controle anti-loop
- nunca repetir a mesma mensagem ou mesma pergunta em sequência
- se a IA gerar resposta idêntica à anterior, o backend deve suavizar e forçar progressão

6. Encerramento
- status encerrado deve bloquear continuidade
- não continuar triagem depois de encerrado

==================================================
REFATORAÇÃO ESPERADA
==================================================

1. Refatorar /openclaw_decision_bridge_v2_1.py para:
- carregar bot_rules dinamicamente
- carregar decision_rules dinamicamente
- carregar KB por cliente dinamicamente
- usar cache com reload por mtime
- aplicar guardrails mínimos antes da IA
- aplicar controle anti-loop
- aplicar humanização/formatter de saída no backend
- manter IA forte na interpretação, mas não dominante em regra crítica
- impedir edição do core por IA
- preservar contrato atual da API

2. Estruturar novos arquivos de configuração:
- /config/bot_rules.md ou /config/bot_rules.json
- /config/decision_rules.json
- /kb/clientes/<cliente>/rules.json
- /kb/clientes/<cliente>/kb.json

3. Ajustar /wf_live_latest.json para:
- usar o backend como fonte única de decisão
- remover lógica conversacional duplicada
- carregar estado/contexto por protocol::customer_id
- enviar contexto corretamente ao bridge
- persistir contexto_atualizado após resposta
- rotear por:
  1. status == ENCERRADO
  2. handoff_humano == true
  3. agendar == true
  4. default -> enviar mensagem
- verificar ticket existente antes de abrir outro
- trocar tokens hardcoded por variáveis de ambiente
- manter o n8n como orquestrador, não roteador inteligente de conversa

4. Humanização obrigatória no backend
Quero uma camada de formatação final de mensagem no bridge:
- respostas mais naturais
- quebra de linhas quando necessário
- no máximo 1 emoji por mensagem
- estilo WhatsApp
- sem ficar infantil ou exagerado
- o n8n só envia; a Bridge prepara a mensagem

==================================================
RESTRIÇÕES
==================================================

- NÃO mudar contrato da API existente
- NÃO reescrever o sistema inteiro
- NÃO adicionar complexidade desnecessária
- NÃO criar nova arquitetura mirabolante
- NÃO alterar nomes de campos já usados entre n8n e FastAPI
- NÃO deixar IA editar o core
- NÃO manter regra crítica escondida apenas no prompt
- preservar ao máximo nodes úteis já existentes no n8n
- focar em correção estrutural e previsibilidade

==================================================
CRITÉRIOS DE ACEITE
==================================================

1. "oi" -> resposta leve, sem identificação precoce
2. "fala com william" sem frustração -> transição suave, sem handoff imediato
3. "fala com william" com frustração -> handoff + ticket
4. "A impressora não tá funcionando" -> progresso para diagnóstico útil, sem loop genérico
5. a mesma pergunta não se repete em sequência
6. contexto_atualizado é salvo e reaproveitado
7. ticket não duplica
8. alteração em bot_rules reflete sem restart
9. alteração em decision_rules reflete sem restart
10. alteração em KB do cliente reflete sem restart
11. mensagens ao cliente saem mais humanas e legíveis
12. IA não sobrescreve o core Python
13. n8n não mantém lógica conversacional duplicada

==================================================
FORMA DE TRABALHO
==================================================

Antes de editar:
1. leia os dois arquivos completos
2. explique onde está a duplicidade de lógica
3. explique os pontos que hoje causam loop, secura de mensagem e falta de progressão
4. proponha um plano de alteração em etapas
5. só depois implemente

==================================================
ENTREGA ESPERADA
==================================================

1. plano resumido
2. lista de arquivos novos e alterados
3. código final do bridge atualizado
4. estrutura dos arquivos de config/KB
5. código final ou trechos finais relevantes do workflow n8n
6. payload final esperado entre n8n e bridge
7. resumo do fluxo final
8. explicação breve de como funciona o hot reload
9. explicação breve de como funciona a transição suave vs handoff real
10. explicação breve de como o anti-loop foi implementado

Definição de pronto:
- previsível em produção
- configurável sem restart
- sem sobrescrever o core
- mais humano na resposta
- sem loop idiota
- sem lógica conversacional brigando entre bridge e n8n
