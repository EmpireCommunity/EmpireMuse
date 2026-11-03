"""O processo filho que decodifica o logo de uma estação (item 8, logo do painel). Não é importado por ninguém: o ``logo.py`` o abre com ``python -I``.

Roda SEM segredos (``env={}``) e com limites de memória e de CPU definidos ANTES de importar o Pillow. Lê o corpo da imagem na entrada padrão (até 256 KiB) e o
formato já detectado pelo pai no argumento; devolve na saída padrão um PNG RGBA de até 128 px, sem metadados. Qualquer defeito é saída com código diferente de
zero e nada na saída: o pai trata como "sem logo".

Só imagens raster pequenas: o Pillow abre SÓ o formato detectado (``formats=``), nunca outro; a imagem passa de ``MAX_PIXELS`` antes e depois da decodificação; só o
quadro 0; nenhuma tolerância a arquivo truncado.
"""

import resource
import sys

MAX_INPUT = 256 * 1024
MAX_PIXELS = 1 << 20
SIDE = 128
ADDRESS_SPACE = 768 * 1024 * 1024
CPU_SECONDS = 4
FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "ICO")


def _limit() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE, ADDRESS_SPACE))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main() -> int:
    _limit()  # antes de importar o Pillow (o decodificador é a parte arriscada)
    if len(sys.argv) != 2 or sys.argv[1] not in FORMATS:
        return 2
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if not data or len(data) > MAX_INPUT:
        return 3

    import io
    import warnings

    from PIL import Image, ImageFile, PngImagePlugin

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    PngImagePlugin.MAX_TEXT_CHUNK = 4096
    PngImagePlugin.MAX_TEXT_MEMORY = 16384
    warnings.simplefilter("error")  # a "bomba" entre 1x e 2x o limite é só um aviso no Pillow: aqui vira erro

    with Image.open(io.BytesIO(data), formats=[sys.argv[1]]) as image:
        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_PIXELS:
            return 4
        image.seek(0)  # só o quadro 0
        image.load()
        width, height = image.size
        if width * height > MAX_PIXELS:
            return 4
        rgba = image.convert("RGBA")
    rgba.thumbnail((SIDE, SIDE))
    clean = Image.frombytes(
        "RGBA", rgba.size, rgba.tobytes()
    )  # imagem nova: nada do arquivo original (texto, perfil, EXIF) vai junto
    out = io.BytesIO()
    clean.save(out, format="PNG")
    sys.stdout.buffer.write(out.getvalue())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001 - qualquer coisa (memória, bomba, formato quebrado) é "sem logo"; nada vai para a saída de erro
        code = 1
    sys.exit(code)
