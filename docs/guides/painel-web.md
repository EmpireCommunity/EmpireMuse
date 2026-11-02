> EmpireMuse é obra derivada do [MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot) (zRitsu), sob licença GPL-2.0.

# Guia de Configuração e Uso do Painel Web

O **EmpireMuse** possui um painel web integrado que permite a administradores gerenciarem configurações de servidores e usuários ajustarem suas preferências diretamente pelo navegador, com login oficial via Discord.

Este guia orienta o passo a passo para configurar o aplicativo no Discord Developer Portal, declarar as variáveis de ambiente necessárias e operar o painel com segurança em ambientes de desenvolvimento ou produção.

---

## 1. Como Funciona a Arquitetura

O painel web é executado como uma sub-aplicação embutida sob a rota `/painel/`, compartilhando a mesma porta TCP e o mesmo processo do servidor Tornado do bot.

> [!IMPORTANT]
> O servidor web que hospeda o painel é o mesmo utilizado pelo sistema de Rich Presence (RPC). Portanto, o painel web **exige que `RUN_RPC_SERVER=true` esteja ativado** para que o servidor web seja inicializado.

Principais características de funcionamento:
- **Zero Retenção de Tokens**: Ao fazer login com o Discord, o painel lê as informações básicas do perfil e dos servidores que você administra e **revoga imediatamente o token de acesso** no Discord. Nenhum token do Discord é armazenado em banco de dados, cookies ou logs.
- **Sessão em Memória**: Uma sessão autenticada é mantida em memória pelo bot e associada a um cookie assinado de alta entropia.
- **Autorização Real no Servidor**: O acesso aos servidores é validado em tempo real contra o estado do bot no Discord. Para configurar um servidor, o usuário precisa ser dono do servidor ou possuir as permissões `Administrador` ou `Gerenciar Servidor`.

---

## 2. Configuração no Discord Developer Portal

Para que o login via OAuth2 funcione, você precisa obter as credenciais de autenticação e registrar a URL de retorno (*redirect*) na sua aplicação do Discord:

1. Acesse o [Discord Developer Portal](https://discord.com/developers/applications) e faça login na sua conta;
2. Selecione a aplicação do bot principal (**EmpireMuse 1** ou o bot correspondente);
3. No menu lateral esquerdo, vá em **OAuth2**:
   - Em **Client information**, localize o **Client ID** (você pode copiar para a variável `PANEL_OAUTH_CLIENT_ID` ou deixar vazio para que o bot resolva automaticamente o ID do primeiro bot do pool);
   - Localize o **Client Secret**, clique em **Reset Secret** (caso não tenha o valor anotado) e copie-o para `PANEL_OAUTH_CLIENT_SECRET`;
4. Na seção **Redirects**, clique em **Add Redirect** e insira exatamente a URL de retorno baseada no seu `PANEL_BASE_URL`:
   - Em produção: `https://seu-dominio.com/painel/callback`
   - Em desenvolvimento local: `http://localhost:6000/painel/callback` (ajuste a porta conforme configurado em `PORT`)
5. Clique em **Save Changes** na parte inferior da página.

> [!WARNING]
> A URL informada em **Redirects** no portal deve ser idêntica, caractere por caractere, ao valor de `<PANEL_BASE_URL>/painel/callback`. Diferenças como barra final ou protocolo (`http` vs `https`) impedirão a conclusão do login com erro de `invalid_request`.

---

## 3. Variáveis de Configuração (`.env`)

Todas as variáveis do painel possuem prefixo `PANEL_`. A tabela a seguir descreve cada parâmetro, seu valor padrão e seu efeito operacional:

| Variável | Padrão | Obrigatória? | Efeito e Descrição |
| :--- | :--- | :--- | :--- |
| `PANEL_ENABLED` | `false` | Não | Ativa (`true`) ou desativa (`false`) o painel web. Quando desativado, requisições para `/painel` retornam 404 e nada do painel é carregado em memória. |
| `PANEL_BASE_URL` | `''` | **Sim** (se ativo) | URL pública absoluta onde o painel é acessado, sem barra no final (ex.: `https://painel.meudominio.com` ou `http://localhost:6000`). Deve usar HTTPS obrigatório, exceto em localhost com cookies inseguros autorizados. |
| `PANEL_OAUTH_CLIENT_ID` | `''` | Não | Client ID da aplicação OAuth2 no Discord. Quando vazio, o sistema resolve e utiliza automaticamente o ID da aplicação do primeiro bot do pool (`BotPool`). |
| `PANEL_OAUTH_CLIENT_SECRET` | `''` | **Sim** (se ativo) | Client Secret da aplicação no Developer Portal. Segredo crítico: **nunca compartilhe nem versione em repositórios públicos**. |
| `PANEL_COOKIE_SECRET` | `''` | Não | Chave secreta persistente (mínimo de 32 caracteres, recomendável 64 hexadecimais aleatórios) para assinar os cookies de sessão. Se deixada vazia, o bot gera uma chave temporária a cada inicialização (o que invalida todas as sessões ativas ao reiniciar o bot, exigindo novo login de todos). Gere facilmente com: `python -c "import secrets; print(secrets.token_hex(32))"`. NUNCA versione este valor. |
| `PANEL_SESSION_TTL_HOURS` | `12` | Não | Tempo máximo de vida de uma sessão em horas (limites: 1 a 168 horas / 7 dias). Após esse período, o login expira mesmo que o usuário continue ativo. |
| `PANEL_SESSION_IDLE_MINUTES` | `60` | Não | Tempo limite de inatividade em minutos (limites: 5 a 1440 minutos / 24 horas). A cada requisição atendida, a expiração por ociosidade é renovada. |
| `PANEL_TRUST_PROXY` | `false` | Não | Deve ser definido como `true` exclusivamente quando o bot estiver atrás de um proxy reverso confiável (ex.: Nginx, Caddy, Cloudflare, Traefik). **Afeta a porta inteira** (página de status `/`, WebSocket `/ws` do RPC e painel `/painel`). O proxy reverso deve obrigatoriamente sobrescrever e sanitizar os cabeçalhos `X-Forwarded-For`, `X-Forwarded-Proto` e `X-Real-IP`. Atrás da Cloudflare, o IP do visitante vem de `CF-Connecting-IP`; só ligue se o proxy aceitar **apenas** a Cloudflare (ou, atrás de outro proxy, se ele remover o `CF-Connecting-IP` recebido do cliente). Se exposto diretamente sem proxy, mantenha `false` para evitar falsificação de IP (*IP spoofing*). |
| `PANEL_INSECURE_COOKIES` | `false` | Não | Destinado **exclusivamente para testes locais**. Permite a emissão de cookies de sessão sem a flag `Secure` em endereços de loopback (`localhost`, `127.0.0.1`, `::1`). Se ativado com domínio externo, o bot recusa a inicialização com erro de segurança. |

---

## 4. Ambientes de Execução

### 4.1 Testes em Desenvolvimento Local (`http://localhost`)

Para testar o painel no computador pessoal ou VPS de teste sem configurar certificados SSL/TLS:

1. No arquivo `.env`, certifique-se de configurar:
   ```env
   RUN_RPC_SERVER=true
   PORT=6000

   PANEL_ENABLED=true
   PANEL_BASE_URL='http://localhost:6000'
   PANEL_OAUTH_CLIENT_SECRET='seu_client_secret_aqui'
   PANEL_INSECURE_COOKIES=true
   ```
2. No Discord Developer Portal, cadastre o redirect: `http://localhost:6000/painel/callback`;
3. Inicie o bot:
   ```bash
   python main.py
   ```
4. Acesse pelo navegador: `http://localhost:6000/painel/`.

> [!NOTE]
> Em modo inseguro local, os cookies são denominados `painel_sessao` e `painel_login` (sem o prefixo de segurança do navegador `__Host-`).

---

### 4.2 Ambiente de Produção com HTTPS Obrigatório

Em servidores de produção, a comunicação via **HTTPS é estritamente obrigatória**:

1. Configure um nome de domínio com certificado SSL válido (ex.: Let's Encrypt ou Cloudflare);
2. O parâmetro `PANEL_BASE_URL` deve iniciar obrigatoriamente com `https://` (o protocolo `http://` é rejeitado na inicialização caso não esteja em loopback local com cookies inseguros autorizados);
3. Gere um segredo persistente de 64 caracteres hexadecimais para `PANEL_COOKIE_SECRET` executando:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
   Isso assegura que reinicializações do processo do bot não desconectem os administradores autenticados;
4. Ajuste o `.env`:
   ```env
   RUN_RPC_SERVER=true
   PORT=6000

   PANEL_ENABLED=true
   PANEL_BASE_URL='https://painel.meudominio.com'
   PANEL_OAUTH_CLIENT_SECRET='seu_client_secret_aqui'
   PANEL_COOKIE_SECRET='chave_hexadecimal_gerada_com_64_caracteres'
   PANEL_TRUST_PROXY=true
   PANEL_INSECURE_COOKIES=false
   ```
5. Cadastre o redirect no Discord Developer Portal: `https://painel.meudominio.com/painel/callback`;
6. Quando `PANEL_INSECURE_COOKIES=false` e a URL é HTTPS, o sistema ativa automaticamente:
   - Cookies protegidos com prefixo de isolamento de host **`__Host-painel_sessao`** e **`__Host-painel_login`**;
   - Atributos `Secure`, `HttpOnly`, `SameSite=Lax` e `Path=/`;
   - Cabeçalho HTTP `Strict-Transport-Security` (HSTS).

---

### 4.3 Proxy Reverso e `PANEL_TRUST_PROXY`

Se você utilizar um servidor web intermediário (como Nginx, Caddy, Cloudflare ou Traefik) à frente do EmpireMuse:

- **Sobrescrita Obrigatória de Cabeçalhos**: O proxy reverso deve **sobrescrever e sanitizar** os cabeçalhos `X-Forwarded-For`, `X-Forwarded-Proto` e `X-Real-IP` com os dados verificados da conexão do cliente (e, se **não** for a Cloudflare, **remover** o `CF-Connecting-IP` que vier do cliente, senão ele poderá forjar o próprio IP) (em vez de apenas repassar cegamente cabeçalhos vindos da internet, prevenindo ataques de falsificação de IP e bypass de rate limit);
- **Escopo Global do `PANEL_TRUST_PROXY`**: A configuração `PANEL_TRUST_PROXY=true` instrui o servidor Tornado a ler esses cabeçalhos em **toda a porta do servidor web** — afetando a página de status (`/`), o relé de WebSocket do RPC (`/ws`) e o painel (`/painel`);
- **IP do visitante atrás da Cloudflare**: no fluxo padrão da borda até o seu servidor (sem Worker nem transformação de cabeçalhos que os remova ou altere), a Cloudflare envia `CF-Connecting-IP` com o IP do visitante, mas não permite definir `X-Real-IP`/`X-Forwarded-For` por regra; um proxy da frente (ex.: Traefik) costuma preencher esses dois com o IP da **borda** da Cloudflare, e aí todos os visitantes pareceriam o mesmo IP (limite de tentativas do login e logs quebrados). Com `PANEL_TRUST_PROXY=true`, o EmpireMuse usa `CF-Connecting-IP` quando ele é um IP válido (painel, página de status e `/ws`, em um só ponto: `ClientIpRouter` em `empiremuse/web_app.py` e `client_ip_from_headers` em `empiremuse/rules/client_ip.py`); sem o cabeçalho, ou com valor inválido, vale o `X-Real-IP`/`X-Forwarded-For` de sempre. Com `PANEL_TRUST_PROXY=false` o cabeçalho é ignorado, porque um cliente direto poderia forjá-lo. **Só ligue o `PANEL_TRUST_PROXY` atrás de um proxy que aceite apenas conexões da Cloudflare** (ex.: Traefik com mTLS e lista de IPs da Cloudflare); se qualquer outro cliente alcançar a porta, ele poderá enviar um `CF-Connecting-IP` falso;
- **Alinhamento do Limite de Corpo**: O servidor HTTP do bot limita corpos de requisição a 64 KiB (`MAX_BODY_SIZE = 64 * 1024`). Configure o seu proxy reverso com um limite alinhado (por exemplo, `client_max_body_size 64k;` no Nginx);
- **Exposição Direta sem Proxy**: Se o bot estiver exposto diretamente na internet sem proxy, mantenha **`PANEL_TRUST_PROXY=false`** para impedir spoofing de cabeçalhos.

---

## 5. Categorias e Permissões Exigidas

Ao acessar a listagem de servidores em `/painel/servidores`, o painel exibe todos os servidores que você gerencia. Para que um servidor possa ser configurado:
- Pelo menos um bot do pool do EmpireMuse deve estar presente e conectado ao servidor;
- O usuário logado deve ser o **Dono do Servidor**, possuir a permissão de **Administrador** ou a permissão de **Gerenciar Servidor** (`Manage Server`).

O painel organiza as opções em abas temáticas:

1. **Player**:
   - Volume padrão (`default_player_volume`), reprodução automática (*autoplay*), detecção de outros bots no canal de voz e modo restrito de comandos.
2. **DJ**:
   - Seleção e gerenciamento de cargos autorizados de DJ (`djroles`).
3. **Aparência**:
   - Seleção de skins do player (skin normal e estática do bot) e skins globais compartilhadas do servidor.
4. **Links Fixos**:
   - Cadastro, edição, remoção, importação e exportação de botões de links rápidos no player (`fav_links`). Requer que a Jukebox já esteja configurada.
5. **Jukebox**:
   - Configuração assistida do canal de texto ou fórum da Jukebox com controlador fixo (`/jukebox setup`, que sempre limpa o canal depois de uma confirmação), as chaves "Apagar mensagens" e "Texto vira pedido", ou desligamento total (`/jukebox off`) com confirmação em duas etapas e acompanhamento de tarefas assíncronas em tempo real. O endereço antigo `/canal` redireciona para `/jukebox`.
6. **Geral**:
   - Prefixo personalizado do servidor, modelo de status automático no canal de voz e remoção de convites de "ouvir junto" ativos (a remoção de convites de ouvir junto exige adicionalmente a permissão de `Gerenciar Canais`).

### Área do Usuário ("Minha Conta")

No menu superior, o usuário pode acessar `/painel/conta` para configurar suas opções pessoais (independentes de servidor):
- **Prefixo Pessoal**: Definição de prefixo exclusivo para acionar o bot;
- **Favoritos**: Gestão completa de faixas e playlists favoritas (com suporte a importação e exportação de backups);
- **Integrações**: Gestão e remoção de integrações vinculadas à conta;
- **Last.fm**: Ativação ou desativação de scrobble automático (a vinculação de conta continua sendo realizada pelo comando no Discord);
- **Token RPC**: Geração segura de token de autenticação para o aplicativo desktop de Rich Presence (exibido apenas uma vez na tela após gerado).

---

## 6. Limitações da Versão Inicial (v1)

Para garantir máxima segurança operacional e estabilidade, alguns recursos foram delimitados nesta primeira versão:

- **Skins Personalizadas**: A criação e edição de skins personalizadas (`G-14`) permanece temporariamente restrita aos comandos do Discord, devido ao formato binário legado que será migrado na fase de saneamento;
- **Criação de Convites de "Ouvir Junto"**: A adição de novos links de ouvir junto (`G-15`) continua nos comandos do Discord (o painel permite listar e remover);
- **Adição de Integrações por URL Direta**: A importação e remoção são suportadas no painel, mas o cadastro avulso de novas URLs externas (`U-03`) deve ser feito via Discord;
- **Sessões Voláteis**: O reinício do processo do bot limpa as sessões ativas da memória, exigindo novo clique em "Entrar com Discord".
