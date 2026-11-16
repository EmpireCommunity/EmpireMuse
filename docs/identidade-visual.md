# Identidade Visual — EmpireMuse

Este documento descreve as diretrizes de identidade visual do **EmpireMuse**, a paleta de cores "Nebulosa", os arquivos de marca oficiais e as instruções para aplicação no Discord Developer Portal.

---

## 1. Paleta de Cores "Nebulosa"

A identidade visual do EmpireMuse adota o conceito cósmico **Nebulosa**, combinando degradês de tons frios e quentes com o violeta como cor primária de destaque.

| Papel | Hex | Uso principal |
|---|---|---|
| Fundo (espaço profundo) | `#0B0A1F` | Fundo base escuro e contraste cósmico |
| Azul | `#3B82F6` | Transição fria e detalhes luminosos |
| **Violeta (cor principal / embeds)** | `#8B5CF6` | Cor primária da marca e padrão dos embeds |
| Magenta | `#C026D3` | Transição cromática e destaques vibrantes |
| Vermelho | `#E11D48` | Acentos quentes e energia visual |

---

## 2. Arquivos de Marca Oficiais (`assets/brand/`)

Todas as peças foram concebidas como arte original do EmpireMuse, sem dependência de ativos de terceiros ou logotipos do upstream.

- **`assets/brand/logo-256.png`**: Logo em PNG de **256×256** pixels (cerca de 31 KB, sem metadados), derivado do `avatar.png`. É o logo usado na interface: cabeçalho do `README.md` e página pública de status (via `branding.asset_url`). Leve de propósito, para não pesar a cada visita.
- **`assets/brand/logo.svg`**: Emblema vetorial, **arquivo-fonte** da marca (não é mais usado na interface). Não contém scripts, estilos externos ou elementos dinâmicos. É um traçado vetorizado detalhado e pesado (cerca de 6 MB), por isso não serve de favicon nem de logo de página.
- **`assets/brand/avatar.png`**: Ícone em formato PNG com resolução de **1326×1326** pixels (cerca de 2,5 MB), usado como avatar do bot e fonte do `logo-256.png`. O emblema ocupa o centro do quadro, o que mantém a legibilidade em tamanhos reduzidos (32–40 px).
- **`assets/brand/banner.png`**: Banner panorâmico em formato PNG com resolução de **2738×959** pixels (cerca de 2,4 MB), para a personalização do perfil no Discord. O Discord exibe o banner reduzido e recortado: mantenha o conteúdo importante no centro.
- **`assets/brand/avatares/`**: Avatares alternativos para trocar a foto do bot: `Avatar-EmpireMuse_1.png` (1600×1600, PNG) e `Avatar-EmpireMuse_2.jpg` a `Avatar-EmpireMuse_5.jpg`.
- **`assets/brand/favicon.ico`**: Ícone do navegador, com 9 tamanhos (de 16 a 256 px), usado pelo painel web.

As demais imagens do bot ficam em `assets/providers/` (ícones das plataformas), `assets/player/`, `assets/skins/` (prévias das skins), `assets/ui/` (ícones da interface e tutoriais), `assets/docs/` (imagens do README) e `assets/emojis/` (emojis de aplicação). O nome de cada arquivo é único entre as subpastas.

---

## 3. Configuração de Cores nos Embeds (`EMBED_COLOR`)

O bot permite personalizar a cor das mensagens incorporadas (embeds) através da variável de configuração `EMBED_COLOR` no arquivo `.env`:

- **Padrão**: `8B5CF6` (violeta da paleta Nebulosa).
- **Formatos aceitos**: hexadecimal de 6 dígitos, com ou sem `#` (ex.: `8B5CF6` ou `#8B5CF6`), sem distinção de maiúsculas e minúsculas.
- **Cor dinâmica do cargo**: defina `EMBED_COLOR=none` (ou deixe vazio) para que os embeds utilizem a cor do cargo mais alto do bot em cada servidor.

---

## 4. Como Aplicar Avatar e Banner no Discord Developer Portal

Para atualizar a imagem de perfil e o banner do seu bot:

1. Acesse o [Discord Developer Portal](https://discord.com/developers/applications) e faça login com sua conta.
2. Em **Applications**, selecione a aplicação do seu bot.
3. **Ícone da Aplicação**:
   - Acesse a aba **General Information** no menu lateral esquerdo.
   - Clique em **App Icon** e carregue o arquivo `assets/brand/avatar.png`.
   - Clique em **Save Changes**.
4. **Avatar e Banner do Bot**:
   - Acesse a aba **Bot** no menu lateral esquerdo.
   - Em **Icon**, carregue o arquivo `assets/brand/avatar.png`.
   - Em **Banner**, carregue o arquivo `assets/brand/banner.png`.
   - Clique em **Save Changes** no rodapé da tela para aplicar as alterações.

---

## 5. Recomendações para Instâncias Próprias (Self-Hosting)

Quem hospeda a própria instância do EmpireMuse deve se atentar a duas configurações essenciais no arquivo `.env`:

- **Servidor de suporte (`SUPPORT_SERVER`)**: Por padrão, aponta para o servidor oficial EmpireHUB. Quem hospeda a própria instância deve trocar pelo convite do próprio servidor ou deixar vazio (`SUPPORT_SERVER=''`) para não exibir o link no comando `/about` nem nos botões de erro.
- **Cor dos embeds (`EMBED_COLOR`)**: O padrão é o violeta `8B5CF6`. Para restaurar o comportamento clássico e utilizar a cor do cargo mais alto do bot no servidor, defina `EMBED_COLOR=none` (ou deixe vazio).

---

## 6. Padronização de Emojis Unicode

O EmpireMuse utiliza exclusivamente **emojis Unicode padronizados** para controles do player e identificação de fontes de música (YouTube, Spotify, Deezer, SoundCloud, Apple Music, Tidal, Last.fm etc.), em substituição aos emojis personalizados legados que dependiam de servidores externos do autor original e falhavam ao renderizar nos bots do EmpireMuse.

Além dos emojis universais, o nome da plataforma é exibido em texto explicativo onde antes havia apenas o ícone, assegurando renderização estável, acessibilidade a leitores de tela e visualização clara em qualquer tema ou plataforma do Discord.

