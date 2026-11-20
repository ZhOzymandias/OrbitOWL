# OrbitOWL

Microservizio standalone che costruisce ontologie OWL da PDF, TXT e Markdown.
L'API accoda i lavori, il worker interroga un motore di inferenza
`llama-server` compatibile per estrarre sezioni con fonti testuali e restituisce
RDF/XML compilato con checksum. È utilizzabile
da terminale o tramite HTTP, anche senza Sinapsia.

## Prima installazione

Su Linux servono Git, Python 3, Docker con Compose e un motore di inferenza
compatibile. OrbitOWL non scarica i pesi né avvia un modello. Il runner deve
supportare `/health`, `/tokenize` e `/completion` con grammatica GBNF ed eventi
SSE nativi (compresa la conferma della grammatica). Il modello deve supportare
il formato ChatML con un blocco di ragionamento già chiuso. Il profilo di partenza
è Qwen3.8:27b. Un endpoint Ollama /api/chat, da solo, non è compatibile.

Per avere un runner dedicato su questo host, un esempio da adattare alla GPU è:

```bash
llama-server -m /percorso/modello.gguf --host 127.0.0.1 --port 18081 -c 65536 -np 1 -ngl 999
```

Poi avvia OrbitOWL:

```bash
git clone https://github.com/ZhOzymandias/OrbitOWL.git
cd OrbitOWL
python3 scripts/setup.py
sudo docker compose up -d --build
python3 -m scripts.client ready
```

`setup.py` crea `.env` e un token casuale in `.orbitowl/api-token`; crea anche
`.orbitowl/data` per coda e risultati. Se li esegui una seconda volta, il
setup conserva token e configurazione. Il token non appare nel file `.env`.
Puoi modificare `.env` usando [l'esempio](.env.example) come riferimento.
I valori principali sono `ORBITOWL_PORT` (8110) e `ORBITOWL_RUNNER_URL`
(`http://127.0.0.1:18081`). Imposta `ORBITOWL_CONTEXT` al contesto realmente
usato dal runner. `ORBITOWL_MODEL` è un'etichetta per i report: il modello
effettivo e la verifica dei pesi sono responsabilità di chi gestisce il runner.

L'API ascolta solo sull'indirizzo locale. Per accedervi dall'esterno usa un
proxy HTTPS o un tunnel privato. Non pubblicare il token, la cartella dati
o l'endpoint del runner. Se Docker è già accessibile al tuo account puoi
omettere `sudo` nei comandi Docker.

## Uso senza Sinapsia

```bash
python3 -m scripts.client submit manuale.pdf
python3 -m scripts.client status ID_JOB
python3 -m scripts.client download ID_JOB risultato.owl
```

Il primo comando mostra l'ID del lavoro. Scarica l'OWL quando lo stato è
`completed`. Il client controlla lo SHA-256 prima di salvare. Per un indirizzo
o un token diversi, passa `--url` e `--token-file` prima del sottocomando.
OrbitOWL verifica RDF/XML; consistenza inferenziale e fedeltà alle fonti
richiedono controlli separati.

| Metodo e percorso | Funzione |
| --- | --- |
| GET `/health/live` | Processo HTTP raggiungibile |
| GET `/health/ready` | Runner raggiungibile e worker pronto |
| POST `/v1/ontology-jobs` | Accetta ZIP con manifest e originali; richiede `Idempotency-Key` |
| GET `/v1/ontology-jobs/{id}` | Stato e diagnostica del lavoro |
| GET `/v1/ontology-jobs/{id}/artifact` | RDF/XML con header `X-Artifact-SHA256` |
| POST `/v1/ontology-jobs/{id}/cancel` | Richiede l'annullamento |

Tutti gli endpoint tranne `/health/live` richiedono il token Bearer.
Il client è anche un esempio di come preparare un archivio per l'API.

## Aggiornamenti con git pull

Su un host Linux con systemd, dopo la prima installazione attiva l'aggiornamento
automatico **una sola volta**:

```bash
./scripts/install_pull_updates.sh
```

Il meccanismo serve una sola checkout per host. Da quel momento, fai solo
`git pull` nella cartella OrbitOWL. Entro circa un minuto il sistema rileva il
nuovo commit locale di `main`, attende che la coda sia libera, costruisce
l'immagine e sostituisce API e worker. L'API può essere brevemente indisponibile.
Per controllare l'esito usa `sudo journalctl -u orbitowl-update.service -n 45`.
Senza systemd puoi aggiornare quando non ci sono job in corso con
`git pull` seguito da `sudo docker compose up -d --build`.

L'installatore copia il coordinatore in una directory di sistema. Se aggiorni
quel coordinatore stesso, riesegui l'installatore una volta. Cambiamenti al
runner e alla configurazione locale hanno un ciclo distinto dal codice.

## Generazione per sezioni

Il worker divide il testo di ogni pagina in sezioni sovrapposte e invia al
runner una richiesta JSON vincolata per sezione. Verifica la presenza testuale
delle citazioni e salva le sezioni accettate in `<job-id>/sections/`.
Se la risposta è troncata o raggiunge un limite di capacità,
il worker divide la sezione lungo un confine testuale. Un errore nei tipi
o negli ID delle entità, oppure una citazione assente dal testo, consente
una sola correzione guidata dal risultato della validazione. Se anche
questa fallisce, il job si ferma e conserva le sezioni già accettate.
Ogni sezione iniziale e le sue suddivisioni condividono
un massimo di 32 invocazioni, conteggiate anche dopo un riavvio.
Un errore temporaneo del runner consente un solo nuovo tentativo della
stessa sezione. Il report indica documento, pagina, intervallo e motivo
dell'arresto. In caso di lista piena indica anche quale ha raggiunto il limite.

Dopo un'interruzione del worker, i job in corso di questa pipeline tornano in
coda. A parità di configurazione e testo il worker riusa le sezioni validate.
Un job della pipeline 1.2 o 1.3 fermato da `section_attempt_budget_exhausted`
può essere rimesso in coda esplicitamente dopo aver aggiornato il worker alla
1.4. Il worker ripercorre le suddivisioni salvate, rivalida i checkpoint con
lo stesso modello e testo e apre il budget 1.4 per le sezioni da elaborare.
Il primo comando conta le sezioni riusabili senza inferenza. Il secondo
riavvia lo stesso ID solo se ritrova almeno tutte le sezioni indicate come
completate dal report precedente.

```bash
sudo docker compose exec -T worker python -m supernova.worker --check-resume ID_JOB
sudo docker compose exec -T worker python -m supernova.worker --resume ID_JOB
```

Per i job integrati, Sinapsia ha uno stato proprio. Un job già segnato come
fallito in Sinapsia richiede una riattivazione anche sul lato client per
importare l'artefatto completato.

Il compilatore riconcilia le entità con lo stesso tipo, etichetta normalizzata
e ambito. Le relazioni conservano ID documento, pagina PDF e citazione
sull'assioma. La verifica del testo citato è automatica. La pertinenza
semantica della citazione richiede revisione. Sinapsia esegue il controllo
OWL 2 DL e HermiT prima di attivare la release.

## Integrazione facoltativa con Sinapsia

Sinapsia può usare OrbitOWL perché il contratto HTTP è lo stesso. Configura
sul client l'indirizzo raggiungibile e il nuovo token, usando il file privato.
Per provare OrbitOWL accanto a un servizio esistente, mantieni progetti Compose,
porte, token e directory dati separati. Se entrambi puntano al medesimo
runner con un solo slot, evita di avviare contemporaneamente due generazioni.

## Sviluppo

Il prompt è in [supernova/section_prompt.txt](supernova/section_prompt.txt).
Il ramo di lavoro è `main`; non creare altri rami senza richiesta esplicita.

Con Python 3.12:

```bash
python3 -m pip install -r requirements.supernova.lock pytest
python3 -m pytest -q
```
