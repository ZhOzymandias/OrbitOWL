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

## Generazione incrementale per documento

OrbitOWL elabora i documenti di ciascun corpus nell'ordine del manifesto.
Ogni chiamata riceve una sezione del testo. Il modello estrae prima le entità
e poi le relazioni e gli attributi. La grammatica della seconda richiesta
ammette gli identificatori e le combinazioni di tipi previsti dalle entità
estratte. Il modello indica il passaggio che documenta ciascun fatto con un ID.
Il worker recupera il testo originale del passaggio e ne verifica la presenza
nella sezione.
Quando l'output è troncato o la sezione esaurisce la capacità, il worker prova
una suddivisione lungo un confine testuale. Un errore non risolto ferma il job
con il dettaglio del documento, della pagina e dell'intervallo.

Le sezioni validate e gli aggiornamenti per documento restano nella
directory del job. Le estrazioni dei documenti completati sono riusabili
anche da job successivi se identità, testo e configurazione coincidono.
Il compilatore ricostruisce il grafo dai soli documenti nel manifesto
corrente. Unifica le entità a parità di tipo, etichetta normalizzata e
ambito. Può riconoscere alias e sigle attestati nel testo. Le relazioni
conservano ID documento, pagina e citazione. La pertinenza semantica
della citazione richiede revisione.

Dopo un'interruzione del worker, i job in corso tornano in coda e riusano
le sezioni già validate. Per alcuni errori terminali delle pipeline
1.2–1.4 e per citazioni non letterali della 1.6.1 è possibile far ripartire
lo stesso job con il worker aggiornato.
Il primo comando verifica i checkpoint senza inferenza. Il secondo
rimette il job in coda se ritrova almeno le sezioni già completate.

```bash
sudo docker compose exec -T worker python -m supernova.worker --check-resume ID_JOB
sudo docker compose exec -T worker python -m supernova.worker --resume ID_JOB
```

OrbitOWL pubblica l'OWL finale dopo l'ultimo documento. Per i job integrati,
Sinapsia esegue i controlli OWL 2 DL e HermiT prima dell'attivazione.
Un job già segnato come fallito in Sinapsia richiede una riattivazione
anche sul lato client per importare l'artefatto completato.

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
