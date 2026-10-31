# Ollama + Gemma 4: rodando um LLM local e resumindo PDFs

Procedimentos mostrados na live: https://www.youtube.com/live/Xm-MSieGhko

## Índice

1. [Instalar o Ollama](#1-instalar-o-ollama)
2. [Baixar e executar o modelo](#2-baixar-e-executar-o-modelo)
3. [Resumir um arquivo PDF](#3-resumir-um-arquivo-pdf)
4. [Alternativa: rodar com Docker](#4-alternativa-rodar-com-docker)
   - [Subir o container (CPU)](#subir-o-container-cpu)
   - [Com GPU NVIDIA](#com-gpu-nvidia)
   - [Executar o modelo](#executar-o-modelo)
   - [Resumir um PDF com o container](#resumir-um-pdf-com-o-container)
   - [Docker Compose (opcional)](#docker-compose-opcional)
5. [Troubleshooting](#5-troubleshooting)
6. [Observações](#6-observações)

## 1. Instalar o Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Flags do `curl`: `-f` falha em erros HTTP, `-s` silencioso, `-S` mostra erros mesmo no modo silencioso, `-L` segue redirecionamentos.

> Dica: o script é executado direto via `sh`. Se preferir, baixe e leia antes de executar.

## 2. Baixar e executar o modelo

```bash
ollama run gemma4:e4b
```

Na primeira execução o modelo é baixado; depois abre um chat interativo no terminal. Para sair, use `/bye`.

## 3. Resumir um arquivo PDF

Instale o `poppler-utils`, que fornece o `pdftotext`:

```bash
sudo apt install poppler-utils
```

Extraia o texto do PDF e envie ao modelo via pipe:

```bash
pdftotext "RFID, Python e rastreabilidade no agro brasileiro.pdf" - | ollama run gemma4:e4b "Resuma o texto recebido:"
```

Como funciona:

- `pdftotext arquivo.pdf -` converte o PDF em texto e escreve na saída padrão (`-`).
- `|` repassa esse texto como entrada ao `ollama run`.
- A string final é o prompt; o texto do PDF vem junto como contexto.

O nome do arquivo tem espaços e vírgula, por isso precisa ficar entre aspas. Para outro PDF, troque o nome e ajuste o prompt (ex.: `"Liste os pontos principais em tópicos:"`).

## 4. Alternativa: rodar com Docker

Útil para não instalar o Ollama direto no sistema.

### Subir o container (CPU)

```bash
docker run -d --name ollama \
  -v ollama:/root/.ollama \
  -p 11434:11434 \
  ollama/ollama
```

- `-v ollama:/root/.ollama` cria um volume para que os modelos baixados persistam.
- `-p 11434:11434` expõe a API do Ollama em `localhost:11434`.

### Com GPU NVIDIA

Requer o [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) instalado.

```bash
docker run -d --gpus=all --name ollama \
  -v ollama:/root/.ollama \
  -p 11434:11434 \
  ollama/ollama
```

### Executar o modelo

```bash
docker exec -it ollama ollama run gemma4:e4b
```

### Resumir um PDF com o container

O `pdftotext` roda no host e o texto é enviado ao container pelo pipe (`-i` mantém o stdin aberto, sem `-t`):

```bash
pdftotext "RFID, Python e rastreabilidade no agro brasileiro.pdf" - | docker exec -i ollama ollama run gemma4:e4b "Resuma o texto recebido:"
```

### Docker Compose (opcional)

```yaml
services:
  ollama:
    image: ollama/ollama
    container_name: ollama
    ports:
      - "11434:11434"
    volumes:
      - ollama:/root/.ollama
    restart: unless-stopped

volumes:
  ollama:
```

```bash
docker compose up -d
docker compose exec ollama ollama run gemma4:e4b
```

> No WSL2, a GPU só fica disponível no container se o Docker estiver configurado com suporte a GPU (Docker Desktop com integração WSL ou NVIDIA Container Toolkit).

## 5. Troubleshooting

### `ollama: command not found` após instalar
Abra um novo terminal ou rode `hash -r` / `source ~/.zshrc`. Confirme com `which ollama`.

### `could not connect to ollama server` / `connection refused`
O servidor não está rodando. Em sistemas com systemd:

```bash
sudo systemctl status ollama
sudo systemctl start ollama
```

No WSL2 sem systemd habilitado, inicie manualmente em outro terminal:

```bash
ollama serve
```

Para testar a API: `curl http://localhost:11434` (deve responder `Ollama is running`).

### `model "gemma4:e4b" not found` / erro no pull
- Verifique o nome e a tag exatos na biblioteca: https://ollama.com/library
- Baixe explicitamente: `ollama pull gemma4:e4b`
- Liste o que já está instalado: `ollama list`

### Download lento ou interrompido
O `pull` retoma de onde parou; rode o comando de novo. Confira também o espaço em disco (`df -h`), pois modelos ocupam vários GB.

### Falta de memória / modelo muito lento
- Feche outros programas pesados e confira a RAM livre (`free -h`).
- No WSL2, a memória é limitada pelo `.wslconfig` (em `C:\Users\<usuario>\.wslconfig`):

  ```ini
  [wsl2]
  memory=12GB
  ```

  Depois rode `wsl --shutdown` no PowerShell e reabra o WSL.
- Sem GPU o modelo roda na CPU e fica bem mais lento. Veja em qual processador ele está: `ollama ps`.

### GPU não é usada no Docker
- Confirme que o container foi criado com `--gpus=all`.
- Teste o acesso: `docker exec -it ollama nvidia-smi`.
- Se falhar, reinstale/configure o NVIDIA Container Toolkit e reinicie o Docker.

### `port is already allocated` (11434)
Já existe um Ollama ocupando a porta (instalação local ou outro container). Pare-o ou mapeie outra porta no host, ex.: `-p 11435:11434`.

### `pdftotext: command not found`
Instale o pacote: `sudo apt install poppler-utils`.

### O resumo vem vazio ou sem sentido
Provavelmente o PDF é escaneado (sem texto). Confira a extração antes de enviar ao modelo:

```bash
pdftotext arquivo.pdf - | head
```

Se sair vazio, use OCR (ex.: `ocrmypdf`) antes do `pdftotext`.

### `sudo apt` não funciona
O `apt` é específico de Debian/Ubuntu. Em Fedora use `sudo dnf install poppler-utils`; em Arch, `sudo pacman -S poppler`; no macOS, `brew install poppler`.

## 6. Observações

- PDFs escaneados (só imagem) não têm texto extraível pelo `pdftotext`; seria necessário OCR.
- Documentos muito longos podem exceder a janela de contexto do modelo e ser truncados.
