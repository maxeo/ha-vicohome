# VicoHome per Home Assistant

Integrazione custom per camere e campanelli **VicoHome** (piattaforma A4x/Addx), con
**live view nativa WebRTC** nel player di Home Assistant.

Stato: **0.4.0, sperimentale**. Provata il 17/11/2026 con un campanello DB2 (a batteria): live con audio, anche con il campanello su un'altra rete; live registrate e clip archiviate in locale.

## Cosa fa

Per ogni camera dell'account (anche quelle condivise):

| Entità | Fonte |
|---|---|
| Camera con **live** (WebRTC) e immagine fissa | live: signaling via cloud, video diretto camera → browser; immagine: copertina cloud o foto dell'ultimo evento |
| Batteria, segnale Wi-Fi, IP, rete Wi-Fi | `/device/listuserdevices` |
| Ultimo evento (con tag AI, foto e video HLS negli attributi) | `/library/newselectlibrary` |
| Evento recente (on per 2 minuti dopo un evento), in carica, online, sveglio | idem |
| **Evento** `event.*_attivita`: `ring` (suonato), `person`, `vehicle`, `pet`, `package`, `motion`, `live` (live registrata, quando il file è salvato) | **push FCM istantanea** (misurato ~1 s dalla pressione); in riserva i nuovi elementi della libreria (polling 30 s) |
| **Archivio locale** (7 giorni) in `/config/vicohome/<entry>/`: ogni evento finito viene scaricato come MP4 + JPEG, e ogni live guardata viene registrata | eventi: HLS della libreria convertito da ffmpeg senza ricodifica (la traccia audio iniziale blocca il player); live: ffmpeg legge l'RTSP del ponte go2rtc (video copiato, audio in AAC) |
| **Media browser** → VicoHome → camera: archivio locale + clip cloud non ancora scaricate (☁) | archivio servito da `/api/vicohome/archive/...`, clip cloud convertite al volo da `/api/vicohome/clip/...` |

Il polling (ogni 30 s) legge solo il cloud e **non sveglia** le camere a batteria.
La live invece sveglia la camera: tenerla aperta consuma batteria.

## Come funziona la live

Per poter registrare, la live passa da un **go2rtc privato** (il binario incluso in HA, avviato
dall'integrazione su localhost con un server STUN; non tocca il go2rtc gestito da HA):

```
browser ──WebRTC──► go2rtc (porte 11985 API, 18564 RTSP, 18565 WebRTC) ──WebRTC/WHEP──► HA ──wss A4x──► cloud ──► camera
                       │
                       └──RTSP──► ffmpeg ──► /config/vicohome/<entry>/AAAAMMGG-hhmmss_<sn>_live.mp4
```

- go2rtc prende la camera da `/api/vicohome/whep/<chiave>/...` (chiave casuale a ogni avvio): HA inoltra
  l'offerta col signaling A4x (SDP compresso "KISS") e restituisce la risposta della camera.
- La camera risponde sempre audio = mid 0 e video = mid 1, mentre go2rtc offre prima il video: l'offerta
  viene riordinata e la risposta rimappata sui mid di go2rtc.
- Una camera che si sveglia può annunciarsi due volte (`PEER_IN`): in quel caso l'offerta viene reinviata.
- Chiusa la live, ffmpeg chiude il file e il collegamento con la camera viene chiuso (torna a dormire).
- **Microfono acceso** (audio `sendrecv` con SSRC nell'offerta del browser): la live va diretta
  browser ↔ camera, senza registrazione. Lo stesso percorso diretto è la riserva se go2rtc non parte.
  Qui HA fa solo da ponte di signaling: l'audio viene annunciato `sendrecv` con un SSRC e la risposta
  della camera viene riordinata come l'offerta del browser.

## Notifiche push

HA si registra su Firebase Cloud Messaging con l'identità FCM pubblica dell'app Android (come fa
l'integrazione Ring) e passa il token al cloud con `/user/setusertoken` (`msgType` `"101"`).
Le push di VicoHome (`type` `"3"` = campanello, con `serialNumber` e `traceId`) fanno scattare subito
l'entità evento; lo stesso `traceId` arriva poi dalla libreria e viene ignorato.
Il client `firebase-messaging` è esteso perché le push di VicoHome hanno l'header `crypto-key` con più
campi; un watchdog lo riavvia se si ferma. Le credenziali FCM sono in `.storage/vicohome.<entry>.fcm`.

## Account

VicoHome permette **una sola sessione per account**: ogni login scollega lo stesso account
dall'app. Usare un **account secondario** e condividergli le camere dall'app principale.

## Installazione (manuale)

Copiare `custom_components/vicohome` in `/config/custom_components/`, riavviare HA,
poi Impostazioni → Dispositivi e servizi → Aggiungi integrazione → **VicoHome**.

**Opzioni** (⚙ Configura sull'integrazione):
- **Cosa registrare**: campanello suonato, persona, veicolo, animale, pacco, altri movimenti, live aperte da HA
  (scelta multipla; senza "live" la live va diretta camera → browser, senza go2rtc);
- **Conservazione**: da 1 giorno a 1 anno, "Per sempre" (cancellazione manuale da `config/vicohome`) o
  "Personalizzato…" (secondo passaggio con il numero di giorni, fino a 3650).

L'icona è in `custom_components/vicohome/brand/` (HA la usa per le integrazioni custom); il sorgente
vettoriale è `docs/icon.svg`.

Per capire i campi di un modello nuovo: menu dell'integrazione → **Scarica diagnostica**
(dati del cloud con email, token, IP, MAC e URL oscurati).

## Crediti e licenze

- [averagenative/vicolocal](https://github.com/averagenative/vicolocal) (MIT): protocollo di
  signaling e codec SDP (`vico_sdp.py` copiato invariato, vedi `LICENSE.vicolocal`).
- [dydx/vico-cli](https://github.com/dydx/vico-cli) (MIT): login, elenco dispositivi, eventi.
- [TomTje/vicohome-ha](https://github.com/TomTje/vicohome-ha) (MIT): prima integrazione HA.

Progetto non affiliato con VicoHome.

## Da fare

Endpoint presenti nell'app Android (elenco in `docs/endpoints-app.txt`), corpo delle richieste da ricavare:
- sirena: `device/doalarm`, `device/cancelAlarm`;
- impostazioni: `device/getuserconfig` / `device/updateuserconfig`, `device/getDeviceAttributes` /
  `device/modifyDeviceAttributes`, `device/updatepersondetect`;
- campanello: `device/config/querydoorbellconfig` / `updatedoorbellconfig`, `device/mechanical/dingdong/audition`;
- luce: `device/queryWhiteLight`, `device/updateFloodlightSwitch`, `device/updateFloodlightLuminance`;
- risveglio: `device/wakeupDevice`; pan/tilt: `device/rotate`, `device/rotateToPoint`;
