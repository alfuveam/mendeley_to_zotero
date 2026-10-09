# Mendeley → Zotero (com PDFs e anotações)

Script em Python que exporta a sua biblioteca do **Mendeley Reference Manager**
(o aplicativo desktop novo) para o **Zotero**, levando junto:

- as referências (título, autores, data, periódico, DOI, ISSN/ISBN, resumo, tags, palavras-chave, notas);
- as pastas do Mendeley, que viram coleções no Zotero (mantendo a hierarquia);
- os PDFs;
- os **highlights e notas** feitos nos PDFs, recriados como anotações nativas do Zotero
  (editáveis, com a cor e o texto marcado).

O script lê o cache local que o Mendeley mantém no seu computador. **Não precisa de
login, chave de API nem internet.** O Mendeley pode estar aberto enquanto ele roda.

## Requisitos

- Mendeley Reference Manager instalado, com login feito e a biblioteca sincronizada pelo menos uma vez
- Zotero 7 ou mais recente
- Python 3.9 ou mais recente
- git (para instalar uma das dependências)

> Não funciona com o **Mendeley Desktop** antigo (o 1.x), que guarda os dados num formato diferente.

## Instalação

```bash
git clone <url-deste-repositório>
cd mendeley_to_zotero
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

No Windows, troque `.venv/bin/` por `.venv\Scripts\`.

## Uso

### 1. Baixe os PDFs no Mendeley

O script só exporta os PDFs que já estão baixados no computador. Se algum faltar, ele
lista quais são no final da execução. Abra esses PDFs no Mendeley (isso faz o download) e
rode o script de novo.

### 2. Rode o script

```bash
.venv/bin/python mendeley_to_zotero.py
```

Ele mostra um resumo (quantas referências, PDFs e anotações) e cria a pasta `saida/`:

| Arquivo / pasta          | O que é |
|--------------------------|---------|
| `importar_no_zotero.js`  | Script que você vai rodar dentro do Zotero (passo 3) |
| `pdfs/`                  | Cópia dos PDFs originais, com o nome de arquivo que tinham no Mendeley |
| `pdfs_anotados/`         | Os mesmos PDFs com os highlights gravados dentro: abrem marcados em qualquer leitor de PDF |
| `biblioteca.bib`         | Alternativa em BibTeX (veja abaixo) |
| `dados.json`             | Tudo o que foi extraído, para conferência |

### 3. Importe no Zotero

1. **Faça um backup** do Zotero: feche o programa e copie o arquivo `zotero.sqlite` da pasta de
   dados (em *Editar → Configurações → Avançado → Arquivos e pastas → Mostrar diretório de dados*).
2. Abra o Zotero e vá em **Ferramentas → Desenvolvedor → Executar JavaScript**
   (*Tools → Developer → Run JavaScript*).
3. Marque **"Executar como função assíncrona"** (*Run as async function*).
4. Cole o conteúdo inteiro de `saida/importar_no_zotero.js` e clique em **Executar**.

No fim aparece um resumo com o número de itens, PDFs e anotações criados. Se algum item
der erro, ele aparece listado e os demais são importados normalmente.

Tudo fica dentro de uma coleção **"Mendeley"**, com as suas pastas como subcoleções.

**Não apague a pasta `saida/` antes de importar:** o Zotero copia os PDFs de lá.

Pode rodar o script no Zotero mais de uma vez sem duplicar nada: cada item recebe uma linha
`Mendeley-ID: ...` no campo *Extra*, e os que já existem são pulados. Isso permite, por
exemplo, baixar PDFs que faltavam, rodar tudo de novo e importar só o que é novo.

## Opções

```
--mendeley PASTA         pasta de dados do Mendeley, se não estiver no lugar padrão
--saida PASTA            onde criar os arquivos (padrão: ./saida)
--colecao-raiz NOME      nome da coleção que recebe tudo no Zotero (padrão: Mendeley;
                         use "" para colocar as pastas direto na raiz)
--incluir-lixeira        exporta também os itens que estão na lixeira do Mendeley
--anexar-pdfs-anotados   anexa no Zotero os PDFs com anotações gravadas, em vez de
                         criar anotações nativas do Zotero
--perfil ID              escolhe o perfil, se mais de uma conta já usou o Mendeley no computador
```

Pastas de dados padrão do Mendeley:

- Linux: `~/.config/Mendeley Reference Manager`
- Windows: `%APPDATA%\Mendeley Reference Manager`
- macOS: `~/Library/Application Support/Mendeley Reference Manager`

## Alternativa sem JavaScript: BibTeX

Se preferir não rodar código no Zotero, use **Arquivo → Importar** e escolha
`saida/biblioteca.bib`. As referências entram com os PDFs de `pdfs_anotados/` anexados.
Nesse caso os highlights aparecem como marcações do próprio PDF, e não como anotações
editáveis do Zotero. As pastas do Mendeley também não viram coleções.

## Limitações

- Exporta só a biblioteca pessoal; bibliotecas de grupos ficam de fora.
- A cor dos highlights é convertida para a cor mais próxima da paleta do Zotero.
- O texto de cada highlight é extraído do próprio PDF (o Mendeley não guarda esse texto).
  Em PDFs escaneados sem camada de texto, o highlight é criado mas fica sem texto.
- O formato do cache do Mendeley não é documentado e pode mudar em versões futuras.
  Testado com Mendeley Reference Manager 2.130 e Zotero 8, no Linux.

## Outra opção

O próprio Zotero tem uma importação online do Mendeley: **Arquivo → Importar → Mendeley
Reference Manager (importação online)**. Ela usa a sua conta do Mendeley em vez dos
arquivos locais. Vale tentar se este script não funcionar no seu caso.

## Como funciona

O Mendeley Reference Manager é um aplicativo Electron e guarda a biblioteca no IndexedDB
do Chromium. O script:

1. copia o IndexedDB para uma pasta temporária e lê os registros com
   [ccl_chromium_reader](https://github.com/cclgroupltd/ccl_chromium_reader). Os registros
   grandes (documentos e anotações) ficam em arquivos *blob* à parte, que o script decodifica
   diretamente;
2. converte os metadados para os campos do Zotero;
3. com o [PyMuPDF](https://pymupdf.readthedocs.io), extrai o texto de cada highlight e grava
   as anotações nas cópias em `pdfs_anotados/`;
4. gera o `importar_no_zotero.js` com os dados embutidos. Como o Mendeley e o Zotero usam o
   mesmo sistema de coordenadas do PDF, as anotações caem exatamente no mesmo lugar.
