# Channel Bet

Painel local para gerenciar vários canais do YouTube em um só lugar:

- **Canais**: conecte quantos canais quiser (inclusive canais de marca) via login do Google.
- **Estatísticas**: inscritos, views e vídeos de todos os canais somados, com variação de 7 dias.
  Página de cada canal com gráfico diário (YouTube Analytics) e vídeos recentes.
  Coleta automática a cada 6 horas.
- **Postagens**: envie um vídeo, escolha o canal, título, descrição, tags e data. Com data agendada,
  o vídeo sobe privado e o próprio YouTube o publica no horário.
- **Geração com IA**: cria vídeos a partir de um tema usando o
  [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo). O resultado vira um rascunho
  para você revisar e aprovar antes de publicar.

Stack: Python 3.11+ · FastAPI · SQLite · Jinja2.

## 1. Instalar

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

## 2. Criar as credenciais do Google (uma vez só)

1. Acesse <https://console.cloud.google.com/> e crie um projeto.
2. Em **APIs e serviços → Biblioteca**, ative:
   - **YouTube Data API v3**
   - **YouTube Analytics API**
3. Em **Tela de consentimento OAuth**: tipo *Externo*, preencha o nome do app e adicione
   o seu e-mail em **Usuários de teste**.
4. Em **Credenciais → Criar credenciais → ID do cliente OAuth**:
   - Tipo: **Aplicativo da Web**
   - URI de redirecionamento autorizado: `http://localhost:8000/oauth/callback`
5. Baixe o JSON e salve na raiz do projeto como `client_secret.json`.

## 3. Rodar

```powershell
.venv\Scripts\activate
uvicorn app.main:app --port 8000
```

Abra <http://localhost:8000> e clique em **Conectar canal**. Repita para cada canal. Para canais
de marca, escolha o canal na tela de seleção de conta do Google.

## 4. (Opcional) Geração de vídeo

Instale e rode o MoneyPrinterTurbo seguindo o README dele, com a API na porta 8080
(`python main.py` na pasta dele). Configure lá a chave do LLM e a chave do Pexels.
Se ele estiver em outro endereço, ajuste `MPT_URL` no `.env`.

## Limitações importantes

- **Cota da API**: cada projeto Google tem 10.000 unidades/dia. Upload custa caro
  (historicamente 1.600 unidades, cerca de 6 vídeos/dia); leitura de estatísticas custa 1 a 2 unidades.
  Confira os valores atuais no Console do Google, em *Cotas*. Se precisar de mais, peça aumento
  pelo formulário de auditoria do YouTube.
- **Projeto não verificado**: vídeos enviados pela API por projetos que não passaram pela auditoria
  do YouTube ficam **travados como privados**. Para publicar como público pelo app, solicite a
  auditoria (*YouTube API Services – Audit and Quota Extension Form*).
- **Modo de teste do OAuth**: enquanto a tela de consentimento estiver em "Teste", os tokens expiram
  em 7 dias e você precisa reconectar os canais. Publicar o app (mesmo sem verificação) resolve isso.
- **Política de conteúdo**: o YouTube penaliza conteúdo repetitivo ou produzido em massa.
  Por isso todo vídeo gerado por IA passa por revisão manual antes de ir para a fila.
- Os tokens ficam em `data/channelbet.db` no seu PC. Não compartilhe essa pasta.

## Estrutura

```
app/
  main.py        rotas e telas
  services.py    regras: canais, estatísticas, uploads, gerações
  youtube.py     OAuth + YouTube Data/Analytics API
  generator.py   cliente da API do MoneyPrinterTurbo
  worker.py      loop em segundo plano (fila de upload, gerações, coleta)
  db.py          modelos SQLite
  templates/     HTML (Jinja2)
  static/        CSS
```
