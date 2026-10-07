# EspelhoHex Simulador

Simulador da parede de espelhos hexagonais cinéticos (inspirada no *Fluidum*).
Mostra a parede em 3D, deixa configurar cada módulo (universo, endereço DMX,
4 ou 7 canais) e recebe Art-Net da mesa de luz, com a mesma lógica do firmware
dos módulos reais (ArtPoll, ArtAddress e RDM).

## Baixar

Baixe o instalador `.exe` para Windows 10/11 na página de
[**Releases**](https://github.com/evertonbuiu/espelhohex-simulador/releases/latest).

## Atualização online

Os apps instalados consultam o [`atualizacao.json`](atualizacao.json) deste
repositório ao abrir e a cada 6 horas. Para publicar uma versão nova:

1. Altere `app/simulador.py` e/ou `app/espelho-hex-cad.html`.
2. Aumente `APP_VERSION` em `app/simulador.py`.
3. Envie para a branch `main`: o GitHub gera o `atualizacao.json` sozinho.

Mais detalhes em [LEIA-ME.txt](LEIA-ME.txt).
