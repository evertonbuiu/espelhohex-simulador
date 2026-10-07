#!/usr/bin/env python3
# =====================================================================
#  Gera o atualizacao.json a partir da pasta app/
#
#  Rode depois de alterar o simulador ou a página e antes de enviar ao
#  GitHub (o fluxo do GitHub também roda isto sozinho a cada envio):
#
#     python gerar_atualizacao.py "o que mudou nesta versão"
#     python gerar_atualizacao.py --canal https://raw.githubusercontent.com/USUARIO/REPO/main/atualizacao.json
#       (grava app/canal.txt: o endereço que os apps instalados vão consultar)
#
#  A versão vem de APP_VERSION em app/simulador.py: aumente esse número
#  a cada atualização, senão os apps instalados não baixam nada.
# =====================================================================
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
FILES = ["simulador.py", "espelho-hex-cad.html"]       # o que a atualização online troca
OUT = os.path.join(ROOT, "atualizacao.json")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--canal"] and len(args) >= 2:
        with open(os.path.join(APP, "canal.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(args[1].strip() + "\n")
        print("canal.txt gravado:", args[1].strip())
        args = args[2:]
    sys.argv[1:] = args
    src = open(os.path.join(APP, "simulador.py"), encoding="utf-8").read()
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        sys.exit("Não achei APP_VERSION em app/simulador.py")
    versao = m.group(1)

    old = {}
    if os.path.isfile(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except ValueError:
            old = {}
    notas = sys.argv[1] if len(sys.argv) > 1 else (old.get("notas", "") if old.get("versao") == versao else "")

    arquivos = {}
    for name in FILES:
        data = open(os.path.join(APP, name), "rb").read()
        arquivos[name] = {"url": "app/" + name, "sha256": hashlib.sha256(data).hexdigest(), "tamanho": len(data)}

    man = {"versao": versao, "notas": notas, "arquivos": arquivos}
    if old.get("versao") == versao and old.get("arquivos") != arquivos:
        print("Atenção: os arquivos mudaram mas a versão continua %s. Aumente APP_VERSION em app/simulador.py"
              " para os apps instalados baixarem a mudança." % versao)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("atualizacao.json gerado: versão %s, %d arquivos" % (versao, len(arquivos)))


if __name__ == "__main__":
    main()
