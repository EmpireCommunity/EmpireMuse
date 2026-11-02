# ADR-0002: Painel Web Integrado com Login Discord, Templates Tornado e htmx

**Data**: 2026-10-15  
**Status**: Aceito  
**Decisores**: Maestro Orquestrador, Backend Specialist, Frontend Specialist, QA & Test Engineer, DevOps & Dependencies, AppSec Hunter, Devil's Advocate, Git & Documentation Keeper  

## Contexto

O **EmpireMuse** necessitava de uma interface web para que administradores de servidores e usuários configurem opções de reprodução, aparência, canais de pedidos, cargos de DJ, links fixos e preferências de conta, reduzindo a sobrecarga de memorização de dezenas de comandos slash e menus de contexto no Discord.

O projeto já utiliza o servidor HTTP e WebSocket **Tornado** (`web_app.py`) no mesmo processo do bot para exibir a página de status e realizar o relé de dados para o aplicativo de Rich Presence (RPC). Além disso, os ambientes comuns de implantação do bot (VPS econômicas com 512 MB a 1 GB de RAM, contêineres Docker e serviços de hospedagem gerenciada) operam sob fortes restrições de memória e frequentemente oferecem apenas uma única porta pública exposta.

Uma pesquisa aprofundada de mercado e ecossistema, feita na etapa de levantamento do painel, revelou que **não existe dashboard de bot Discord pronto e sustentável** que combine Python/disnake/Tornado com licença de código aberto compatível com a **GPL-2.0** do EmpireMuse:
- Dashboards populares de referência (ex.: MrAugu, LachlanDev, ZyroX) são acoplados a Node.js/`discord.js`, exigem builds de frontend em React/Next.js ou utilizam processos externos;
- Projetos em Python encontrados no ecossistema (como Red-Dashboard) utilizam licenças incompatíveis (AGPL-3.0) ou estão arquivados;
- Outros projetos alternativos adotam licenças restritivas comerciais (Elastic License 2.0 no Zeppelin, "Shareware" CC BY-NC-SA no Discord-Dashboard) ou não possuem licença declarada.

Portanto, tornou-se necessária a tomada de uma decisão arquitetural formal sobre como desenhar, hospedar, autenticar e implementar o painel web do EmpireMuse com segurança máxima, baixo consumo de recursos e sem aumentar a dívida técnica do ecossistema Python.

## Decisão

**Decidimos construir o painel web como uma sub-aplicação Tornado isolada (`panel/`), montada sob o caminho `/painel/` no mesmo processo e loop de eventos asyncio do `BotPool`, utilizando autenticação OAuth2 do Discord com PKCE S256, controle de sessão em memória com cookies seguros `__Host-`, renderização no servidor com templates Tornado e interações dinâmicas via htmx e Pico CSS vendorizados.**

Principais pilares da arquitetura adotada:

1. **Mesmo Processo e Porta Única**:
   - A sub-aplicação em `panel/` é instanciada e roteada por `make_router` dentro de `web_app.run_app()`. Ela compartilha o mesmo loop assíncrono do `BotPool`, permitindo acesso direto aos bots prontos, caches do gateway do Discord e camadas de persistência (`MongoDatabase` e `LocalDatabase`), sem a necessidade de filas externas, threads de sincronização ou IPC inter-processos.
   - O painel é controlado pela variável `PANEL_ENABLED` (padrão `false`) e herda a execução do servidor web atrelada a `RUN_RPC_SERVER=true`.

2. **Fluxo de Autenticação OAuth2 Sem Retenção de Tokens**:
   - Utilização do fluxo *Authorization Code Grant* com geração criptográfica de `state` e par PKCE S256 (`code_verifier` e `code_challenge`), associados a uma tentativa de login de uso único com validade de 10 minutos.
   - Escopos estritamente mínimos: `identify` e `guilds`.
   - Após a troca do código temporário por token e leitura dos endpoints `/users/@me` e `/users/@me/guilds?limit=200`, o token de acesso é **imediatamente revogado** no Discord (`POST /oauth2/token/revoke`). O EmpireMuse **não armazena nem persiste** tokens de acesso ou de atualização de usuários em cookies, bancos de dados ou logs.

3. **Sessões e Cookies Criptografados**:
   - Gerenciamento de sessão em memória (`panel/sessions.py`) atrelado a um identificador de alta entropia (32 bytes aleatórios) transmitido via cookie assinado (`cookie_secret`).
   - Em produção HTTPS, utiliza cookies protegidos pelo prefixo de segurança `__Host-` (`__Host-painel_sessao`), com atributos `HttpOnly`, `SameSite=Lax`, `Path=/` e `Secure`.
   - Suporte a ambiente de desenvolvimento local (`http://localhost`) habilitado explicitamente via flag `PANEL_INSECURE_COOKIES=true`.

4. **Autorização Autoritativa no Servidor com Dados do Bot**:
   - A lista de servidores retornada pelo Discord OAuth é utilizada estritamente como pré-filtro visual.
   - O acesso a qualquer configuração de servidor (`panel/authz.py`) é validado contra o estado real do bot no `BotPool`: o bot precisa estar na guilda, o usuário precisa ser membro atual e possuir permissões de dono do servidor, `administrator` ou `manage_guild`.
   - Leituras utilizam cache curto de até 30 segundos; escritas exigem validação mandatória em tempo real (`fresh=True`).

5. **Interface Server-Driven com htmx e Pico CSS Vendorizados**:
   - Em vez de uma Single Page Application (SPA) pesada, adota-se renderização orientada a hipertexto no servidor (templates Tornado) com trocas parciais de fragmentos HTML via **htmx 2.0.10** (licença 0BSD) e estilo visual base via **Pico CSS 2.1.1** (licença MIT), customizado pelos tokens de design da marca em `panel/static/css/nebulosa.css`.
   - Ambas as bibliotecas são vendorizadas estaticamente em `panel/static/vendor/` com hashes conferidos em `SHA256SUMS`, sem requisições a CDNs externas e sem ferramentas de build Node.js em produção.
   - Política de Segurança de Conteúdo (CSP) rígida: `default-src 'self'`, `script-src 'self'`, `style-src 'self'`, `img-src 'self' https://cdn.discordapp.com data:`, sem uso de `'unsafe-inline'` nem `'unsafe-eval'`.

6. **Controle Otimista de Concorrência e Camada de Serviços**:
   - Cada formulário inclui um campo oculto `versao` calculado a partir do hash SHA-256 canônico dos campos relevantes. Modificações concorrentes (via comando do Discord ou múltiplos administradores) retornam status HTTP 409 (*Conflict*).
   - As regras de negócio do painel foram isoladas em serviços puros (`panel/services/`), que espelham com precisão as regras e limites dos comandos no Discord, com equivalência validada por testes de caracterização. Apenas a lógica de configuração e desligamento da Jukebox foi de fato unificada e extraída para `utils/music/jukebox_channel.py`, compartilhada entre os comandos `/jukebox setup`, `/jukebox off` e as tarefas assíncronas do painel (`TaskRunner`). A migração dos cogs para consumirem diretamente os demais serviços de `panel/services/` permanece como débito técnico mapeado para a fase de saneamento (ver KI-42 em `docs/known-issues.md`).

7. **Escopo Delimitado da v1**:
   - A versão 1.0 foca em configurações de servidores (Player, DJ, Aparência, Geral, Links fixos, Jukebox) e conta de usuário (prefixo pessoal, favoritos, integrações, scrobble Last.fm e token de RPC).
   - Recursos de risco elevado de segurança ou que demandam reestruturação prévia (skins personalizadas com serialização binária legada `G-14`, adição de convites de "ouvir junto" `G-15` e adição de integrações por URL `U-03`) foram postergados para a fase de saneamento.

## Alternativas Consideradas

### Alternativa 1: Single Page Application (SPA) em React / Next.js / Vue com API REST JSON Pública
- **Prós**: Amplo ecossistema de componentes prontos de UI e dissociação completa entre frontend e backend.
- **Contras**: Exigência de pipeline de compilação Node.js e gerenciador de pacotes npm em ambientes de produção; aumento significativo do consumo de memória RAM do contêiner; duplicação de modelos de validação (Python no bot e TypeScript no browser); necessidade de projetar, versionar e autenticar uma API JSON pública ampla sujeita a abusos e varreduras automatizadas.
- **Por que não**: Aumentaria drasticamente a complexidade operacional para usuários comuns que executam o bot via VPS modesta ou scripts locais, violando a premissa de simplicidade e baixo consumo de recursos.

### Alternativa 2: Aplicação Web em Processo Separado (FastAPI / Quart / Flask) com IPC
- **Prós**: Isolamento total de falhas entre o servidor HTTP e a execução do bot; ecossistema abundante de extensões prontas de autenticação para Starlette/FastAPI.
- **Contras**: Exigência de uma segunda porta TCP ou proxy reverso intermediário obrigatório; necessidade de protocolo de comunicação entre processos (IPC via sockets de domínio Unix, WebSockets locais ou Redis) para consultar o estado dos bots e aplicar efeitos em tempo real (ex.: recarregar skins, alterar status de voz); overhead de conexões duplicadas ao MongoDB.
- **Por que não**: O Tornado já está instalado e ativo no processo do `BotPool`. Aproveitar o mesmo loop de eventos assíncrono elimina latência de rede inter-processos e não adiciona nenhuma dependência extra ao `requirements.txt`.

### Alternativa 3: Adoção / Adaptação de Dashboards Existentes do Ecossistema Discord
- **Prós**: Reaproveitamento teórico de código e fluxos de navegação já testados por outros bots.
- **Contras**: Conflitos intransponíveis de licença (o Zeppelin utiliza a Elastic License 2.0, que proíbe oferta como serviço; Red-Dashboard é AGPL-3.0 e incompatível com o isolamento pretendido; Discord-Dashboard possui restrições comerciais de shareware); a quase totalidade dos projetos ativos exige Node.js/`discord.js`.
- **Por que não**: Risco jurídico imediato de violação da licença GPL-2.0 do projeto e incompatibilidade com o framework `disnake`.

### Alternativa 4: Framework CSS Dinâmico com Build ou Alternativas Mínimas (Simple.css, Tabler)
- **Prós**: Simple.css é extremamente leve (~9 KB) e ativo; Tabler oferece componentes de dashboard altamente sofisticados.
- **Contras**: Simple.css não oferece componentes estruturais de painel (alternadores *switch*, modais semânticos `<dialog>`, acordeões e grids de formulários); Tabler e Tailwind exigem utilitários de compilação e dependências Node.js.
- **Por que não**: O Pico CSS 2.1.1 é puramente estático, sem qualquer JavaScript, e entrega formulários e alternadores acessíveis nativos em apenas 83 KB (~11,6 KB comprimido). Embora seu repositório original tenha sido arquivado pelo autor em 04/10/2026, seu código foi vendorizado sob licença MIT e protegido por hash SHA-256. Caso surjam incompatibilidades futuras com novos navegadores, o Simple.css permanece catalogado como plano B direto.

## Consequências

### Positivas
- **Dependências Zero em Produção**: Nenhuma biblioteca Python adicional foi introduzida no `requirements.txt` (Tornado e aiohttp já eram utilizados pelo bot);
- **Segurança Reforçada contra Exfiltração de Dados**: A revogação imediata de tokens OAuth do Discord e o uso exclusivo de IDs de sessão efêmeros em cookies `__Host-` impedem que o comprometimento de um banco ou cookie exponha tokens permanentes de usuários do Discord;
- **Defesa Robusta contra CSRF e Clickjacking**: Proteção nativa XSRF em todos os formulários e cabeçalhos estritos (`frame-ancestors 'none'`, `X-Frame-Options: DENY`, CSP sem eval/inline);
- **Auditoria Transparente**: Todas as alterações de configuração são registradas estruturadamente em log com escopo, servidor e nomes dos campos alterados, sem jamais imprimir segredos ou tokens;
- **Consistência Operacional**: O compartilhamento real da lógica da Jukebox (`utils/music/jukebox_channel.py`) unifica o comportamento do `/jukebox setup` e `/jukebox off`, enquanto testes de caracterização asseguram paridade entre os comandos existentes e os serviços do painel (`panel/services/`) até a refatoração completa planejada no saneamento (KI-42).

### Negativas
- **Sessões em Memória**: O reinício do processo do bot invalida as sessões ativas dos administradores no painel web, exigindo uma nova autenticação via Discord;
- **Limitação Temporária de Escopo**: Usuários que utilizam skins personalizadas legadas ou desejam cadastrar novos links de "ouvir junto" ainda precisam utilizar temporariamente os comandos no Discord até a conclusão das etapas de saneamento;
- **Manutenção de CSS Vendorizado**: O congelamento do Pico CSS v2.1.1 exige que correções de estilo pontuais sejam feitas na camada própria `nebulosa.css`.

### Riscos e Mitigações
- **Risco**: Colisão de alterações simultâneas entre moderadores no Discord e administradores na web.
  - **Mitigação**: Campo `versao` baseado em hash SHA-256, `asyncio.Lock` granular por escopo e retorno HTTP 409 com mensagem instrutiva de recarregamento.
- **Risco**: Esgotamento de limites de requisição (*rate limiting*) da API do Discord em IPs compartilhados (ex.: provedores de computação em nuvem com IPs compartilhados).
  - **Mitigação**: Limitação de taxa em memória por IP nas rotas de login/callback (`panel/ratelimit.py`), minimização das chamadas à API do Discord (apenas no login e sob demanda estrita) e verificação de autorizações ancorada na memória interna dos bots do pool.
- **Risco**: Envenenamento de cabeçalhos de proxy (*Host Header / IP Spoofing*).
  - **Mitigação**: O parâmetro `PANEL_BASE_URL` é validado e fixado na configuração; cabeçalhos `X-Forwarded-*` só são aceitos quando a flag `PANEL_TRUST_PROXY=true` estiver explicitamente ativa atrás de proxies confiáveis.

> **Nota (2026-11):** Canal de pedidos → Jukebox; modo 24/7 → Radios. Os nomes e caminhos acima já seguem a nomenclatura nova.
