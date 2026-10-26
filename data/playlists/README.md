# Playlists importáveis

Esta pasta guarda listas de playlists que os administradores de um servidor poderão importar pelo comando
`/playlists importar`. Cada arquivo `.json` daqui é uma lista importável; hoje há `playlists-empire.json`, a lista
oficial da comunidade (somente URLs públicas).

> **Recurso ainda não implementado** (item 17 do planejamento). Os arquivos já ficam no repositório para não se perderem.

## Formato

Um objeto com a chave `playlists`, que é uma lista de playlists. Chaves em inglês; `order` define a posição.
Exemplo vazio (mantido aqui no README, e não em `.json`, para não aparecer na lista de arquivos importáveis):

```json
{"playlists": [{"order": 1, "name": "", "description": "", "genres": [], "url": ""}]}
```

| Campo | Tipo | Descrição |
| :--- | :--- | :--- |
| `order` | número | Posição da playlist na exibição. |
| `name` | texto | Nome da playlist. |
| `description` | texto | Descrição curta. |
| `genres` | lista de textos | Gêneros musicais. |
| `url` | texto | Link público da playlist. |

## Regras previstas

- O corpo do arquivo é validado antes de qualquer gravação (formato, tipos e tamanhos).
- A importação só acrescenta: nunca sobrescreve playlists já existentes no servidor.
- Não duplica: playlist com o mesmo nome ou a mesma URL de uma já existente é ignorada.
- Os dados ficam só no banco, por servidor; não há variável de ambiente para isso.
