# ADR-0006: Painel externo substitui o genérico somente no modo externo

**Data**: 2026-11-11
**Status**: Aceito
**Decisores**: Dono do projeto, Regente e Alicerce

## Contexto

O painel da comunidade usa uma API com listener próprio e o painel genérico continua necessário para hospedagem própria.
A decisão de runtime deve governar as rotas e os links anunciados, sem recarregar segredos para montar URLs.
O contrato da API permanece em `docs/api/openapi.yaml`; a API e o redirect ainda dependem do servidor do relé e de
`RUN_RPC_SERVER=true`.

## Decisão

Com as variáveis `EXTERNAL_PANEL_*` vazias, preservamos o painel genérico configurado pelo operador. Quando o listener
externo abre, desligamos o genérico e substituímos `/painel` e seus subcaminhos por redirect `302` apenas em `GET`/`HEAD`
para a base externa validada. Configuração externa inválida ou listener bloqueado não ativa o genérico como alternativa.
Perfil, presença padrão e status anunciam o endereço externo somente após a abertura do listener. `run_app` registra a
URL pública no pool; o perfil e a presença leem esse estado sem importar a configuração da API nem carregar segredos.

O redirect não transporta caminho, query, corpo, `code` nem `state`. Todos os erros respondem `404 Not Found`, sem cookies
ou OAuth antigo. O destino nunca vem de cabeçalhos ou parâmetros do visitante. Os logs omitem query e corpo; HTTPError
esperado não gera ERROR. O IP de `/ws` e status depende exclusivamente de `PANEL_TRUST_PROXY` e dos CIDRs confiáveis,
mesmo sem painel genérico. Os CIDRs são analisados uma vez no boot e compartilhados com o genérico.

## Alternativas Consideradas

- Desligar o genérico em toda instalação: impediria a hospedagem própria, contrariando a decisão do dono.
- Reativar o genérico se a API falhar: reabriria uma interface que o operador pediu para substituir.
- Separar o redirect do relé: amplia o ciclo de vida dos listeners; fica fora desta mudança.

## Consequências

### Positivas

- Uma única URL de runtime impede anúncio externo quando o bind falha ou o relé está desligado.
- API e genérico compartilham sessões, serviços e um único TaskRunner por pool.
- Configuração de proxy independe da presença do painel genérico.

### Negativas

- Sem `RUN_RPC_SERVER=true`, a API externa e o redirect não abrem.
- A operação de release precisa remover o redirect_uri antigo `/painel/callback` no portal do Discord e preservar
  `/panel/auth/callback`; esta ação não é executada pela alteração do código.

### Riscos e Mitigações

- Formulários ou códigos OAuth poderiam vazar ao novo host: apenas GET/HEAD redirecionam para destino fixo, sem parâmetros,
  com `Cache-Control: no-store` e `Referrer-Policy: no-referrer`.
- Clientes poderiam forjar IP: CF-Connecting-IP só é aceito de pares TCP nos CIDRs confiáveis; CIDRs inválidos falham fechados.
- Tarefas poderiam ser encerradas duas vezes: o shutdown do genérico é dono do runner compartilhado quando ele existe;
  sem genérico, o shutdown da API o encerra.

## Verificação

Testes HTTP de métodos, callbacks e destinos hostis; fiação dos modos ativo, bloqueado e próprio; URLs anunciadas pelo
runtime; CIDRs e IP do relé/status com genérico ausente ou inválido; estado e shutdown compartilhados; Ruff e mypy.
