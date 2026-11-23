"""O processo filho que decodifica a capa de uma faixa (skins de imagem). Não é importado por ninguém: ``covers.py`` o abre
com ``python -I``, sem ambiente (``env={}``), em ``/`` e com a saída de erro descartada.

Os limites de memória, CPU, arquivos e core são aplicados ANTES de importar o Pillow. Lê o corpo na entrada padrão (até 2 MiB)
e o formato já detectado pelo pai no argumento (só JPEG, PNG e WEBP); devolve na saída padrão exatamente 240 × 240 pixels RGB
crus (172.800 bytes), com o recorte central e a redução já feitos. O pai nunca roda decodificador sobre bytes de fora.

- o Pillow abre SÓ o formato detectado (``formats=``);
- ``MAX_IMAGE_PIXELS`` (2048²) conferido antes e depois do ``load()``; aviso de bomba vira erro;
- sem tolerância a arquivo truncado; limites de texto do PNG; só o quadro 0; JPEG reduzido já na decodificação (``draft``);
- sem perfil ICC e sem ``exif_transpose`` (capa girada é aceitável; EXIF é superfície de parse).

Qualquer defeito é saída com código diferente de zero e nada na saída: o pai trata como "sem capa".
"""

import resource
import sys

MAX_INPUT = 2 * 1024 * 1024
MAX_PIXELS = 2048 * 2048
SIDE = 240
ADDRESS_SPACE = 768 * 1024 * 1024
CPU_SECONDS = 3
FORMATS = ("JPEG", "PNG", "WEBP")


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
        if image.format == "JPEG":
            image.draft("RGB", (SIDE, SIDE))  # decodifica já reduzido (escala 1/2, 1/4 ou 1/8, nunca abaixo do alvo)
        image.load()
        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_PIXELS:
            return 4
        rgb = image.convert("RGB")
    side = min(width, height)
    left, top = (width - side) // 2, (height - side) // 2
    square = rgb.crop((left, top, left + side, top + side)).resize((SIDE, SIDE), Image.Resampling.LANCZOS)
    pixels = square.tobytes()
    if len(pixels) != SIDE * SIDE * 3:
        return 5
    sys.stdout.buffer.write(pixels)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001 - qualquer coisa (memória, bomba, formato quebrado) é "sem capa"; nada vai para a saída de erro
        code = 1
    sys.exit(code)
