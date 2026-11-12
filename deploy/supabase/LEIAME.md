# Arquivos do Supabase das soluções

O portal cria um Supabase para cada solução com `[supabase]` no manifesto (ver
`marketplace/supabase.py` e a seção "Supabase das soluções" do README). Esta pasta guarda só os
arquivos que ele copia para dentro dos containers ao criá-los (`docker cp`):

- `volumes/db/*.sql`: scripts de inicialização do banco (senhas dos papéis internos, validade dos
  tokens, webhooks). Rodam só na primeira vez, com o volume de dados vazio.
- `volumes/api/envoy/*`: configuração do gateway (Envoy).

São os arquivos do compose oficial do Supabase, no commit indicado em `ORIGEM-COMMIT`, sem
alteração. Para atualizar: troque os arquivos e as versões das imagens em `marketplace/supabase.py`
(`IMAGENS`) pelos do mesmo commit novo. Ao mudar um arquivo daqui, o portal recria o container que
o usa no próximo Iniciar (os dados ficam nos volumes).
