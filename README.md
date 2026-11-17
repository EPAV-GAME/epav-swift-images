# EPAV Swift Images

Robô diário de imagens Swift. Os produtos, as revisões de sincronização e as cópias de produtos arquivados ficam no PostgreSQL Neon, acessado pela API EPAV. As fotos ficam no Cloudflare R2. Este robô não usa chave privada Firebase e não consulta o Firestore.

## Execução

O workflow `Sincronizar imagens Swift` mantém execução diária às 09:17 UTC e execução manual com simulação, limite, somente produtos sem foto, limpeza de duplicados e verificação de ausentes. `SYNC_ENABLED=true` habilita a tarefa. Requer os segredos `DATA_SYNC_TOKEN` e `IMAGE_SYNC_TOKEN` e a variável `IMAGE_SERVICE_URL`.

A API do catálogo é `https://epav-product-evaluator.kevinernandes2012.workers.dev`. O segredo de sincronização permite apenas as rotas `/v1/sync/*`; os tokens de jogadores não dão acesso a elas. A API invalida Redis após cada alteração de produto, mesmo se uma etapa posterior do robô falhar.

## Comportamento preservado

- Consulta o catálogo oficial Swift e busca por código, nome, peso, marca e contexto; resultados ambíguos são mantidos para análise.
- Usa ETag/Last-Modified e hash para evitar baixar ou publicar fotos inalteradas.
- Normaliza WebP para 512 × 512 e até 100 KB; o arquivo no R2 é identificado por SHA-256.
- Preserva fotos manuais do administrador.
- Deduplicação e remoção de ausentes exigem varredura completa sem erros ou metadados incompletos. Há cópia recuperável no PostgreSQL.
- Arquivamento e exclusão ocorrem na mesma transação, com revisão do produto e do registro canônico. Edições concorrentes interrompem a operação.
- A restauração usa `/v1/sync/restore` e recusa substituir um produto existente.

## Testes

`pip install -r requirements.txt`

`PYTHONPATH=src python -m unittest discover -s tests -v`

Os relatórios do Actions não contêm dados comerciais nem credenciais. A modelagem e os scripts estão em [epav-modelagem](https://github.com/EPAV-GAME/epav-modelagem).
