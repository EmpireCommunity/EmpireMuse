"""Geração de tokens RPC compartilhada pelo comando e pelo painel."""

import secrets
import string
from collections.abc import Callable

TOKEN_LENGTH = 50
TOKEN_ALPHABET = string.ascii_letters + string.digits


def generate_rpc_token(choice: Callable[[str], str] = secrets.choice) -> str:
    return "".join(choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))
