# Imagens Swift — EPAV

Serviço independente do jogo e do painel. Consulta diariamente o sitemap público da Swift, extrai os produtos e suas fotos do JSON-LD e relaciona com `produtos_swift` no Firebase `epav-game`.

## Correspondência

Prioriza o código original Swift quando presente no nome do arquivo oficial. Caso contrário, exige o mesmo nome normalizado, mantendo peso, corte e variantes. Correspondências ambíguas ou inexistentes não recebem fotos; a imagem anterior é preservada. O catálogo completo, suas margens e as credenciais nunca são publicados neste repositório.

## Imagens

- WebP, 512 × 512 pixels, fundo branco, proporção preservada.
- Até 100 KiB por arquivo; qualidade ajustada até cumprir o limite.
- Bucket privado R2 `epav-swift-images`; leitura pública pelo Worker.
- Chave `swift/<sha256>.webp`: versões imutáveis, arquivos iguais compartilhados.
- Usa ETag/Last-Modified; compara pixels quando o servidor exige download. Upload e campo do produto só mudam se a foto mudar, estiver ausente ou o padrão de conversão mudar.
- Respeita robots.txt, espera pelo menos um segundo por host, não acessa a busca proibida e não contorna bloqueios do site.

## Firebase

Adiciona somente `imagemSwift` ao produto: URL, bucket, chave, hash, formato, dimensões, tamanho, origem e data. Usa a versão do documento como precondição, protegendo contra edição simultânea. A coleção privada `sincronizacao_imagens_swift` guarda verificações e validadores HTTP. As classificações e os dados comerciais são preservados.

O Worker aceita apenas imagens WebP 512 × 512 com hash correto e até 100 KiB. Upload exige `IMAGE_SYNC_TOKEN`; não oferece listagem nem exclusão. Arquivos antigos permanecem no bucket.

## Implantação

1. `cd worker && npm ci`
2. Autorizar Wrangler na conta Cloudflare correta.
3. `npx wrangler r2 bucket create epav-swift-images`
4. `npx wrangler secret put IMAGE_SYNC_TOKEN` (chave aleatória forte).
5. `npm run deploy`
6. No GitHub Actions, configurar os segredos `FIREBASE_SERVICE_ACCOUNT_JSON` e `IMAGE_SYNC_TOKEN` e as variáveis `IMAGE_SERVICE_URL` e `SYNC_ENABLED=true`.

O segredo Firebase permite operações privilegiadas. Nunca colocá-lo nos arquivos do projeto. A chave de upload deve ser a mesma no GitHub e no Worker.

## Execução diária

Workflow **Sincronizar imagens Swift** às 09:17 UTC (06:17 em São Paulo), além de execução manual. O GitHub pode atrasar o horário; em repositórios públicos, desativa agendamentos após 60 dias sem atividade. O relatório contém apenas contadores. Páginas removidas (404) ou sem metadados utilizáveis são contabilizadas em `pagesSkipped` e identificadas nos logs pela URL pública, sem causar falha geral. Erros reais de consulta (`pageErrors`), upload ou Firebase (`errors`) continuam sinalizando execução com erro. Nenhuma dessas situações apaga fotos anteriores. Se nenhuma página fornecer produtos utilizáveis, a execução falha.

```powershell
$env:PYTHONPATH='src'
$env:FIREBASE_SERVICE_ACCOUNT_FILE='C:\caminho\service-account.json'
$env:IMAGE_SERVICE_URL='https://epav-swift-images.SEU-SUBDOMINIO.workers.dev'
python -m swift_images.sync --dry-run --limit 20
python -m unittest discover -s tests -v
```

Execução real exige `IMAGE_SYNC_TOKEN`. `--limit 0` processa todos os alimentos. O limite se aplica aos alimentos, não à indexação pública. Produtos fora do catálogo atual da Swift podem permanecer sem imagem.

Para verificar ou repetir um alimento específico, informar juntos `--product-code` e `--source-page`. A página precisa estar no sitemap oficial e sua foto precisa conter o código informado. O mesmo recurso está disponível na execução manual do workflow.

```powershell
python -m swift_images.sync --product-code 616920 --source-page https://www.swift.com.br/file-de-peito-de-frango-swift-1kg/p --dry-run
```
