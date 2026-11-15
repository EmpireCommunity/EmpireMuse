"""API do bot para painéis externos (``/api/v1``): listener e aplicação Tornado próprios, sem relação com ``web_app``.

O painel externo (servidor Node, BFF) fala com o bot por aqui, com a chave de serviço mais um token de sessão emitido pelo
bot. O navegador nunca fala com esta API, e o bot nunca chama o painel.
"""
