# Resumos locais com Ollama + Gemma

Chat local para **resumir PDF, TXT e MD** com modelos do [Ollama](https://ollama.com) rodando no seu computador, **sem enviar nada para a nuvem**. Você pode comparar vários modelos no mesmo arquivo, e cada resultado fica salvo num banco SQLite com um relatório em HTML.

## Requisitos

- Docker com Docker Compose.
- Uns 10 GB livres em disco para os modelos e 8 GB ou mais de RAM (o modelo maior usa cerca de 7 GB).
- Internet na primeira vez, para baixar os modelos. Depois disso o resumo funciona offline. Só a aparência das páginas depende da internet (veja "Problemas comuns").
- Não precisa de GPU. Tudo roda na CPU, só que mais devagar.

## Subir o serviço

Na pasta do projeto:

```bash
docker compose up -d --build
```

Isso sobe o Ollama, baixa o modelo `gemma4:e4b` uma vez (cerca de 5,5 GB, pode demorar) e sobe o chat. Para subir só o chat e o Ollama, sem baixar nada:

```bash
docker compose up -d --build chat
```

Abra **http://localhost:8000**.

Para parar: `docker compose down`. Os modelos e o banco continuam guardados.

## Usar o chat

1. **Escolha os modelos.** Os botões no rodapé listam os modelos já instalados. Clique para marcar ou desmarcar. Com **um** modelo você recebe um resumo; com **dois ou mais**, uma comparação. Para usar um modelo que ainda não está na lista, digite o nome (por exemplo `gemma3:12b`) no campo "outro modelo" e aperte Enter. Ele é baixado automaticamente na primeira vez.
2. **Anexe o arquivo.** Clique em "Escolher arquivo" ou arraste o arquivo para a área tracejada. Formatos aceitos: **PDF, TXT e MD**, até 20 MB.
3. **Clique em "Gerar relatório".** O arquivo aparece como mensagem sua, e cada modelo ganha um cartão que mostra o resumo sendo escrito ao vivo. Os modelos rodam **um de cada vez**; os que ainda não começaram aparecem como "na fila".
4. **Acompanhe a barra de progresso**, no rodapé de cada cartão, até a análise terminar:
   - ao baixar um modelo novo, a barra mostra o percentual real do download;
   - ao carregar o modelo e ler o texto, ela fica em movimento contínuo (não há como saber quanto falta);
   - durante a geração, ela avança conforme os tokens são escritos. Esse avanço é uma **estimativa**, porque o tamanho final do resumo não é conhecido, e por isso ela não passa de 95% antes de a análise concluir. Ao terminar, a barra completa e some.
5. **Abra o relatório.** Ao terminar surge o botão "Abrir relatório", que mostra o relatório numa janela (modal) sobre o chat.
6. **Comece outra.** O botão **Nova análise**, ao lado de "Abrir relatório", limpa a conversa e o arquivo escolhido e mantém os modelos marcados.

O chat avisa quando o arquivo não pode ser usado: tipo errado, PDF escaneado (sem texto), arquivo vazio, binário disfarçado de texto ou maior que o limite.

## Menu, histórico e ajuda

O botão de **três pontinhos** (⋮), no topo do chat, abre um menu com **Histórico**, **Ajuda** e o **status do Ollama**. Histórico e Ajuda abrem numa janela (modal) sobre o chat. O status mostra uma bolinha **verde** (online, com a versão e o número de modelos instalados) ou **vermelha** (offline). 

O status se mantém **sempre atualizado** com o [htmx](https://htmx.org): a página consulta o Ollama ao carregar, **a cada 10 segundos** (enquanto a aba está visível), ao voltar para a aba e ao abrir o menu. Se o próprio chat sair do ar, a bolinha fica vermelha com "Servidor do chat inacessível".

**Clicar no item de status:**

- com o Ollama **online**, abre um modal com os **modelos instalados**: parâmetros, quantização, família, contexto máximo, capacidades (por exemplo `completion`, `vision`, `thinking`), tamanho em disco, data de atualização e se o modelo está **na memória** agora (com o tamanho e a hora em que será descarregado). No topo há a versão do Ollama, o total de modelos, o espaço em disco ocupado e quantos estão na memória;
- com o Ollama **offline**, o clique tenta consultar de novo. Para fechar, use o ✕, a tecla `Esc` ou clique fora da janela. O botão "Abrir em nova aba" abre a mesma página fora do chat.

O **Histórico** lista todos os resultados, do mais recente para o mais antigo: número, data, arquivo, tipo, se foi resumo ou comparação e os modelos usados. **Clique em uma linha** para abrir o relatório daquele registro dentro da mesma janela, com o botão "← Voltar" para retornar à lista.

## O relatório

Cada relatório mostra o arquivo (tipo, tamanho, caracteres extraídos e contexto usado) e uma **matriz de comparação** com uma linha por modelo:

| Coluna | O que significa |
|---|---|
| Modelo | Nome do modelo. |
| Tipo | Tipo do arquivo de entrada: pdf, txt ou md. |
| Carga | Tempo para colocar o modelo na memória. É alto na primeira vez e quase zero depois, enquanto o modelo continua carregado (10 minutos). |
| Leitura | Tempo para o modelo processar o texto de entrada. |
| Geração | Tempo para escrever o resumo, com a velocidade em tokens por segundo. |
| Resumo | O texto completo gerado pelo modelo. |

Separar carga, leitura e geração evita comparar modelos pelo tempo errado: um modelo grande parece lento só porque foi o primeiro a ser carregado.

## Modelos sugeridos

| Modelo | Tamanho | Quando usar |
|---|---|---|
| `gemma3:270m` | 291 MB | Testes rápidos. **Não é confiável para resumir**: costuma escrever em primeira pessoa ou inventar. |
| `gemma3:1b` | 806 MB | Rascunho rápido. Pode errar detalhes. |
| `gemma3:4b` | 3,4 GB | Bom equilíbrio entre tempo e qualidade. |
| `gemma4:e4b` | 6,6 GB | O mais completo e o mais lento. |

Os tempos dependem da sua máquina. Em um notebook sem GPU, um PDF de uma página levou de poucos segundos (modelos pequenos) a alguns minutos (`gemma4:e4b`).

## Como funciona e limites

- **Um job por vez.** Se você enviar outro enquanto um roda, ele espera na fila. Rodar modelos em paralelo na CPU não ganha tempo e ainda falseia as medições.
- **Resultado reproduzível.** Os modelos rodam com `temperature 0` e o modo de raciocínio desligado (`think: false`).
- **Contexto ajustado ao texto.** O chat calcula o tamanho de contexto de que o arquivo precisa. Se o texto for grande demais (acima de 16384 tokens), o relatório traz um **aviso** de que o modelo viu só parte dele.
- **Resumo cortado.** O resumo tem teto de 1024 tokens. Se o modelo chegar ao limite, a linha aparece marcada como "cortado".
- **Execução cancelada não é salva.** Se você fechar a aba no meio de uma geração, o job para e nada é gravado.
- **Idioma.** O pedido é sempre resumir em português, sem saudação nem comentário final.

## Onde ficam os dados

Tudo em `outputs/resumos.db` (SQLite), no host. Para consultar sem instalar nada no host, use o Python do container do chat:

```bash
docker compose exec chat python -c "
import sqlite3
for row in sqlite3.connect('/outputs/resumos.db').execute(
    'SELECT run_id, model, round(gen_s,1), round(tok_s,1) FROM results'):
    print(row)"
```

Cada linha é `(execução, modelo, segundos de geração, tokens por segundo)`. Se você tiver o `sqlite3` no host, também pode abrir `outputs/resumos.db` direto com ele.

Para começar do zero, pare o chat e apague o arquivo `outputs/resumos.db`; ele é recriado na próxima inicialização.

## Configuração

Variáveis do serviço `chat` (no `docker-compose.yml`):

| Variável | Padrão | Função |
|---|---|---|
| `OLLAMA_HOST` | `http://ollama:11434` | Endereço do Ollama. |
| `OUT_DIR` | `/outputs` | Pasta onde fica o banco. |
| `DB_PATH` | `$OUT_DIR/resumos.db` | Caminho do banco. |
| `PORT` | `8000` | Porta dentro do container. |

A porta no seu computador é a primeira de `127.0.0.1:8000:8000`. Mude-a se a 8000 estiver ocupada. O chat só aceita conexões da própria máquina.

Para usar GPU NVIDIA, instale o NVIDIA Container Toolkit e descomente o bloco `deploy` do serviço `ollama`.

## Problemas comuns

- **"Ollama indisponível".** Veja `docker compose ps` e `docker compose logs ollama`. O serviço `ollama` precisa estar `healthy`.
- **O primeiro uso de um modelo demora.** Ele está sendo baixado (a barra de progresso aparece no cartão). Se o download travar, rode `docker exec ollama ollama pull <modelo>`; ele retoma de onde parou.
- **"PDF sem texto extraível".** O PDF é uma imagem escaneada. É preciso passar por OCR antes (por exemplo com `ocrmypdf`).
- **Muito lento.** Sem GPU o modelo roda na CPU. Use um modelo menor, feche outros programas e confira a memória livre. No WSL2, a memória é limitada pelo `.wslconfig`.
- **Página sem estilo ou resumo sem formatação.** O visual vem de bibliotecas carregadas da internet (Tailwind, htmx e renderizador de Markdown). Sem rede o conteúdo continua legível, só mais simples, e o status do Ollama mostra "Status indisponível (htmx não carregou)".
- **Porta 8000 ou 11434 em uso.** Troque o número da porta do lado esquerdo no `docker-compose.yml`.

## Uso pela linha de comando (opcional)

O chat é uma API simples. Para enviar um arquivo sem a interface:

```bash
curl -N -X POST -H 'X-Upload: 1' --data-binary @arquivo.pdf \
  'http://localhost:8000/api/summarize?models=gemma3:1b,gemma3:4b&filename=arquivo.pdf'
```

A resposta é uma linha de JSON por evento (início de cada modelo, texto gerado, tempos). O último evento, `{"type":"done","report":"/reports/N"}`, indica o relatório em `http://localhost:8000/reports/N`. O cabeçalho `X-Upload: 1` é obrigatório.

## Estrutura do projeto

```
docker-compose.yml        serviços: ollama, pull-model, chat
app/server.py             servidor, extração de texto, chamadas ao Ollama, SQLite
app/static/index.html     tela do chat (cartões, barras de progresso, menu e modal)
app/static/head.html      tema compartilhado (Tailwind + tokens do shadcn/ui)
app/Dockerfile            imagem do chat (Python + poppler-utils)
outputs/resumos.db        banco com os resultados
```

O shadcn/ui não oferece versão de CDN. As páginas usam o Tailwind via CDN com as cores e as classes de componentes do shadcn, escritas à mão.

## Outros arquivos da pasta

- `ollama-gemma4-pdf.md`: passo a passo do Ollama com Gemma 4 mostrado na live.
- `hacktoberfest-victor-gemma4.txt`: links do evento (live, slides, notebooks e guias do Gemma).
- `RFID, Python e rastreabilidade no agro brasileiro.pdf` e `.docx`: documento de exemplo para testar.
