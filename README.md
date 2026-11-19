# OrbitOWL

Servizio di generazione di ontologie OWL su Supernova. Riceve PDF o testo, accoda
il lavoro, invoca il runner locale e restituisce RDF/XML con hash. Sinapsia su
Sirio continua a inviare i lavori, validare con HermiT e importare le ontologie.

- `supernova/ontology_prompt.txt` è il prompt da modificare.
- `supernova/` contiene API, coda, worker e adattatore al runner.
- `compose.supernova.yml` esegue API e worker sul progetto Compose esistente.
- `tests/` contiene i test del servizio e dell'aggiornamento.

Il ramo di lavoro e di rilascio è `main`. Non creare rami aggiuntivi senza
richiesta esplicita. Il formato delle richieste e la revisione dell'API sono
quelli già usati da Sinapsia. Nessun cambio di configurazione su Sirio.

## Passaggio dal servizio esistente a OrbitOWL

Su Supernova, come `trooperai`, dopo aver pubblicato il repository privato:

```bash
git clone https://github.com/ZhOzymandias/OrbitOWL.git ~/OrbitOWL
cd ~/OrbitOWL
./scripts/install_pull_updates.sh
```

Per il clone privato usa credenziali GitHub già configurate o una chiave di deploy
di sola lettura e il corrispondente URL SSH. Lo script richiede sudo per registrare
l'aggiornamento automatico. L'installazione esistente deve essere avviata e pronta.
Lo script collega la configurazione privata già presente in
`~/sinapsia-supernova/.supernova`. Il progetto Compose continua a chiamarsi
`sinapsia-supernova` per sostituire i container API e worker esistenti sulla
stessa porta. Il runner GPU resta gestito dalla sua unità systemd attuale.

**Mantieni la vecchia cartella.** Contiene dati, token e configurazione del
runner. L'aggiornamento non rimuove o riscarica i pesi e non avvia inferenze.
Non lanciare due copie del servizio. Il prompt attivo dopo l'adozione è 3.2.

## Uso quotidiano

Su Supernova:

```bash
cd ~/OrbitOWL
git pull
```

Entro circa un minuto systemd rileva il nuovo commit locale e applica
l'aggiornamento se API e runner sono pronti. Se ci sono job in coda o attivi,
controlla di nuovo al giro successivo. Prima prepara l'immagine, poi sostituisce
solo API e worker. Il sistema non scarica commit da solo. I riavvii del runner e
le modifiche ai pesi o al contesto restano operazioni di infrastruttura separate.
Durante la sostituzione l'API può essere brevemente indisponibile.

Per verificare lo stato o diagnosticare un eventuale errore:

```bash
sudo journalctl -u orbitowl-update.service -n 45 --no-pager
```

Dopo un errore durante la sostituzione, l'aggiornamento prova a rimettere le
immagini precedenti se la coda è libera. Il commit fallito resta sospeso fino
al pull di un commit diverso. Dati e coda sono condivisi con la vecchia
installazione. Il pacchetto è stato testato localmente senza Docker/GPU;
la prima adozione su Supernova è il collaudo operativo.

## Verifica locale

Con Python 3.12:

```bash
python3 -m pip install -r requirements.supernova.lock pytest
python3 -m pytest -q
```
