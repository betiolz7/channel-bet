# Channel Bet

Painel local para gerenciar vários canais do YouTube e contas do Instagram em um só lugar:

- **Canais**: conecte quantos canais quiser (inclusive canais de marca) via login do Google.
- **Instagram**: conecte contas Profissionais e publique o mesmo vídeo como Reel junto com o YouTube.
  Seguidores, posts e views dos posts recentes aparecem no painel.
- **Estatísticas**: inscritos, views e vídeos de todos os canais somados, com variação de 7 dias.
  Página de cada canal com gráfico diário (YouTube Analytics) e vídeos recentes.
  Coleta automática a cada 6 horas.
- **Postagens**: arraste o vídeo, escolha um ou **vários canais**, título, descrição, tags e data.
  Com data agendada, o vídeo sobe privado e o próprio YouTube o publica no horário.
  Filtros por status e por canal.
- **Calendário**: visão mensal das publicações, colorida por canal. Clique em um dia para agendar.
- **Sugestão com IA (Claude)**: gera 3 opções de título, descrição e tags a partir do tema.
  Requer `ANTHROPIC_API_KEY` no `.env` (opcional).
- **Geração de vídeo com IA**: cria vídeos a partir de um tema usando o
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

## 4. (Opcional) Instagram

A publicação usa a API do Instagram com **Login do Facebook**, a única forma de enviar o vídeo
direto do PC (o "Login do Instagram" exige o vídeo em um servidor público).

**Antes de começar**, para cada conta:

- Mude a conta para **Profissional** (Criador de conteúdo ou Empresa): app do Instagram → Configurações →
  Tipo de conta e ferramentas.
- Vincule a conta a uma **Página do Facebook** que você administra (pode criar uma Página só para isso):
  Instagram → Editar perfil → Página, ou pela Central de Contas.

**Criar o app da Meta (uma vez só):**

1. Acesse <https://developers.facebook.com/apps> → **Criar app**.
2. Caso de uso: **Gerenciar mensagens e conteúdo no Instagram** (ou tipo de app **Empresa**).
3. Escolha a configuração **"API com Login do Facebook"**. Adicione as permissões `instagram_basic`,
   `instagram_content_publish`, `instagram_manage_insights`, `pages_show_list`,
   `pages_read_engagement` e `business_management`.
4. Em **Login do Facebook → Configurações**, adicione o URI de redirecionamento
   `http://localhost:8000/oauth/meta/callback`.
5. Se o app pedir uma **Configuração** do Login do Facebook para Empresas, crie uma com
   "Token de acesso do usuário" e as permissões acima, e copie o ID para `META_CONFIG_ID`.
6. Em **Configurações do app → Básico**, copie o ID e a Chave secreta para o `.env`:
   `META_APP_ID` e `META_APP_SECRET`.
7. Deixe o app em **modo de desenvolvimento**. Nesse modo, só quem tem função no app
   (administrador, desenvolvedor ou testador) consegue conectar, e não precisa de revisão da Meta.

Reinicie o app e clique em **Conectar Instagram**. Na tela do Facebook, marque as Páginas e as contas
do Instagram que quer usar. Todas as contas marcadas entram de uma vez.

**Como a postagem funciona no Instagram:**

- O vídeo é publicado como **Reel** (MP4/MOV, 3 s a 15 min, de preferência 9:16).
- Legenda: campo próprio na nova postagem. Se ficar vazia, usa título + descrição.
- **A API não agenda.** O app publica no horário marcado, então o PC precisa estar ligado e o app aberto.
  Se estiver desligado, o post sai assim que o app abrir.
- Limite da Meta: 100 posts por conta a cada 24 horas.
- "Views" no painel é a soma dos 50 posts mais recentes (o Instagram não fornece o total da conta).

## 5. (Opcional) Geração de vídeo

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
  instagram.py   Login do Facebook + API do Instagram (Reels, estatísticas)
  generator.py   cliente da API do MoneyPrinterTurbo
  worker.py      loop em segundo plano (fila de upload, gerações, coleta)
  db.py          modelos SQLite
  templates/     HTML (Jinja2)
  static/        CSS
```
