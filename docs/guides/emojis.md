# Emojis do Bot

Os botões, menus e mensagens do EmpireMuse usam ícones próprios (plataformas de música, controles do player, fila). Como o bot pode rodar em qualquer servidor, esses ícones são **emojis de aplicação**: imagens que ficam na aplicação do bot no Discord e valem em qualquer servidor, sem precisar de convite nem de permissão de emoji.

---

## 1. Como Funciona

1. Ao ficar pronto, **cada bot principal** (`TOKEN` e `TOKEN_BOT_*`) envia para a **própria aplicação** no Discord as imagens da pasta `assets/emojis/*.png` que ainda não existem lá.
2. Isso acontece uma vez por inicialização do processo e é seguro repetir: um emoji que já existe com o mesmo nome é usado como está, e o bot **só cria o que falta**. Ele nunca apaga nem altera nenhum emoji da aplicação, nem os que não são dele.
3. Cada mensagem usa os emojis do bot que a envia: um emoji de aplicação só aparece quando quem manda a mensagem é a própria aplicação.

Os arquivos precisam ser PNG (ou GIF animado) de 128×128 com até 256 KiB e constar no `assets/SHA256SUMS`; um arquivo ausente, inválido ou com hash diferente não é enviado, e o bot registra um aviso.

### Reserva em Unicode

Enquanto os emojis não terminam de subir, ou quando um deles não pôde ser criado (arquivo ausente, falta de permissão, limite de envio da API), o bot usa o **emoji Unicode** equivalente no lugar. Nada quebra: o ícone original aparece assim que o emoji de aplicação existir.

A aplicação do Discord comporta até 2000 emojis; o EmpireMuse usa no máximo 64.

---

## 2. Trocar um Ícone (`emojis.json`)

O dono pode trocar os ícones das plataformas de música por outros emojis com um arquivo `emojis.json` em `RUNTIME_DIR` (ou na raiz do projeto), por exemplo:

```json
{
  "music_sources": {
    "youtube": "<:meu_youtube:123456789012345678>",
    "spotify": "<:meu_spotify:123456789012345679>"
  }
}
```

As chaves são os nomes das fontes (`youtube`, `soundcloud`, `spotify`, `deezer`, `applemusic`, `twitch`, `jiosaavn`, `tidal`, `youtubemusic`, `last.fm`, `bandcamp`). O valor do `emojis.json` **vence** o emoji de aplicação e a reserva Unicode. Hoje só a chave `music_sources` é lida.

---

## 3. Bots de Servidor (`APP_EMOJIS_GUILD_BOTS`)

| Chave | Padrão | O que faz |
| :--- | :--- | :--- |
| `APP_EMOJIS_GUILD_BOTS` | `false` | Com `true`, os bots **de servidor** do `guild_bots.json` também enviam os emojis para a aplicação deles. |

Por padrão só os bots principais enviam emojis; os bots de servidor usam a reserva Unicode e não chamam a API. Ligue a chave só se **você controla as aplicações** desses bots: o `guild_bots.json` pode conter bots de outras pessoas, e ligar a chave cria até 64 emojis em cada uma delas, ocupando parte do limite de 2000 que o dono daquele bot não escolheu ocupar.
