# KidsHub – Protokoll zwischen App und HA-Integration

Stand: 2026-11-06. Dieser Vertrag gilt verbindlich für beide Pläne (Integration und App).
Er wird als `docs/protocol.md` in beide Repos kopiert. Änderungen erfolgen nur hier und
werden in beiden Repos nachgezogen.

Produktname **KidsHub**, HA-Domain `kidshub`.

## 1. Grundsätze

- Die App spricht für Steuerung und Daten **ausschließlich** mit der Integration. Sie hat keine
  HA-Benutzerdaten und keinen Zugriff auf die MA-API.
- **Audio** empfängt die App als **Sendspin-Player** direkt von MA (Rolle `player`, Codec
  **PCM**). Adresse und Client-ID bekommt sie über `config.sendspin`. Sendspin transportiert
  nur den Audiostrom und die Lautstärke-Befehle. Auswahl, Fortsetzen und Bewertungen laufen
  über die Integration.
- Auth erfolgt über ein **Geräte-Token** (32 Byte, zufällig, URL-safe base64), das beim
  Koppeln erzeugt wird. Es gilt nur für die Endpunkte unter `/api/kidshub/`.
- Alle Zeiten sind lokal in der HA-Zeitzone, Format `"HH:MM"`. Wochentage: `0` = Montag …
  `6` = Sonntag.
- Lautstärken sind ganze Zahlen von `0` bis `100`. Helligkeiten ebenso, außer bei
  Licht-Entitäten (dort `0..255`).
- Die Bezeichner von MA-Inhalten sind **MA-URIs** (`library://track/12`,
  `spotify://album/xyz`, …) und für die App undurchsichtig.

## 2. HTTP-Endpunkte (HA-Views, `requires_auth = False`, eigene Token-Prüfung)

| Methode & Pfad | Auth | Zweck |
|---|---|---|
| `POST /api/kidshub/pair` | Kopplungscode im Body | Gerät koppeln |
| `GET /api/kidshub/ws` | erste WS-Nachricht | WebSocket |
| `GET /api/kidshub/img?u=<urlencoded>&s=<px>` | `Authorization: Bearer <token>` | MA-Bild laden, auf `s`×`s` (max. 512) verkleinern, JPEG q=80 |
| `GET /api/kidshub/tts/<sha256>.<ext>` | `Authorization: Bearer <token>` | gecachte TTS-Datei (`ext` wie von der HA-TTS-Engine geliefert, z. B. `mp3`/`wav`) |

### 2.1 Koppeln
Request: `{"code": "123456", "app_version": "1.0.0", "device_model": "..."}`
Response 200: `{"device_id": "<uuid4 hex, = unique_id des Config-Entries>", "token": "<token>", "child_name": "Paul"}`
Response 403: `{"error": "invalid_code"}` (falscher oder abgelaufener Code, Gültigkeit 10 Min)
Nach 10 Fehlversuchen werden alle offenen Kopplungen verworfen (Schutz gegen Durchprobieren). Antworten tragen `Cache-Control: no-store`.

## 3. WebSocket

Jede Nachricht ist ein JSON-Objekt mit `"type"`. Requests der App tragen eine `"id"` (int).
Die Antwort darauf lautet `{"type":"result","id":N,"success":true,"result":{...}}` bzw.
`{"type":"result","id":N,"success":false,"error":{"code":"...","message":"..."}}`.
Fehlercodes: `not_allowed`, `not_found`, `ma_unavailable`, `invalid`, `tts_failed`.

### 3.1 Handshake
App → `{"type":"auth","device_id":"…","token":"…","app_version":"1.0.0"}`
Server → `{"type":"auth_ok"}` und direkt danach `config`. Danach je ein `entity_state` für
jede Zimmer-Entität und immer ein `now_playing` (leer mit `kind:null`, wenn nichts läuft).
Bei ungültigem Token oder unbekanntem Gerät: `{"type":"auth_invalid"}`, danach schließt der Server — die App gibt dann endgültig auf (neu koppeln). Ist das Gerät bekannt, aber gerade nicht geladen (Neuladen, HA-Start), schließt der Server **ohne** `auth_invalid`; die App verbindet sich mit Backoff neu.
Ping/Pong übernimmt die WS-Ebene (Heartbeat 30 s). Die App verbindet sich mit Backoff neu:
1, 2, 5, 10, 30 s, danach alle 30 s.

### 3.2 Server → App (Push)

**`config`** (vollständig, bei jeder Änderung neu gesendet; `version` zählt nur innerhalb eines HA-Laufs hoch und beginnt nach einem HA-Neustart wieder bei 1 — die App vergleicht Versionen nicht, sondern übernimmt immer die letzte empfangene Config)
```json
{"type":"config","version":17,
 "child_name":"Paul",
 "tiles":{"music":true,"audiobooks":true,"podcasts":true},
 "settings_visible":false,
 "music_enabled":true,
 "max_volume":60, "alarm_max_volume":40,
 "alarm":{"enabled":true,"time":"07:00","weekdays":[0,1,2,3,4],"lead_minutes":15,
          "with_light":true,"button_required":false},
 "sleep_timer":{"window_start":"19:00","window_end":"06:00","minutes":30},
 "night":{"start":"19:30","end":"06:30","brightness":5},
 "day_brightness":70,
 "sendspin":{"url":"ws://192.168.1.10:8927/sendspin","client_id":"kidshub-<device_id>","name":"KidsHub Paul"},
 "room":[{"entity_id":"light.paul_decke","name":"Deckenlicht","icon":"mdi:ceiling-light",
          "kind":"dimmer","colors":[]},
         {"entity_id":"light.paul_lese","name":"Leselampe","icon":"mdi:lamp",
          "kind":"color","colors":[[255,92,92],[255,210,63],[76,217,123],[155,107,255],[58,160,255],[255,243,214]]}]}
```
`sendspin.url` leitet die Integration aus der von MA selbst gemeldeten Basis-Adresse ab (`/info` → `base_url`; Host, Port
`8927`, Pfad `/sendspin`), nicht aus der Entry-URL (die kann ein Container-Name sein). Fallbacks: MA-`/info` über HTTP, bei MA als Add-on die IP des HA-Hosts. Ein in den KidsHub-Einstellungen gesetztes `sendspin_url` hat immer Vorrang. Fehlt MA, ist `sendspin` `null`. Die App nutzt `client_id` als
Sendspin-Client-ID (MA vergibt eine eigene `player_id`, siehe §3.3).

`kind` ∈ `switch` | `dimmer` | `cover` | `color`.
Die Zuordnung macht die Integration: `light` mit Farbmodus `hs`/`rgb*`/`xy` → `color`,
sonstiges dimmbares `light` → `dimmer`, `cover` → `cover`, alles andere → `switch`.

**`entity_state`**
`{"type":"entity_state","entity_id":"light.x","on":true,"brightness":180,"rgb":[255,0,0],"position":null,"available":true}`
`brightness` ist `0..255` oder `null`, `position` (Cover) `0..100` oder `null`.

**`now_playing`** (bei jedem Titelwechsel und alle 10 s während der Wiedergabe)
```json
{"type":"now_playing","kind":"track"|"episode"|"audiobook"|"album_track",
 "uri":"…","title":"…","subtitle":"…","image_url":"/api/kidshub/img?u=…",
 "duration_ms":183000,"elapsed_ms":42000,"stars":4,"container_uri":"…",
 "container_kind":"album"|"audiobook"|"playlist"|"podcast"|null}
```
`container_kind` ist optional (ab Integration 0.1.1); fehlt es, ermittelt die App die Art aus früheren `browse`-Ergebnissen.
Ist die Queue leer: `{"type":"now_playing","kind":null}`.

**`alarm_cmd`**: `{"type":"alarm_cmd","cmd":"stop"}`
**`sleep_cmd`**: `{"type":"sleep_cmd","cmd":"cancel"}` | `{"type":"sleep_cmd","cmd":"set","minutes":20}`

### 3.3 App → Server

**`state`** (Push ohne `id`, bei jeder Änderung, höchstens 1× pro Sekunde; während des
Sonnenaufgangs alle 15 s)
```json
{"type":"state","playback":"playing"|"paused"|"idle","volume":45,
 "alarm_state":"off"|"sunrise"|"ringing"|"stopped","sunrise_progress":0.42,
 "sleep_remaining_s":812,"screen":"day"|"night"|"sunrise"}
```
`sleep_remaining_s` ist `null`/fehlt, wenn kein Timer läuft. `sunrise_progress` wird nur in `sunrise`/`ringing` gesendet und fehlt sonst; Empfänger müssen das Fehlen tolerieren.

**Requests (mit `id`)**

| type | Felder | Ergebnis |
|---|---|---|
| `browse` | `section`: `music` \| `audiobooks` \| `podcasts`; optional `parent_uri`; `sort`: `default` \| `stars` | `{"items":[Item]}` |
| `favorites` | `kind`: `track` \| `episode` \| `audiobook` | `{"items":[Item]}` (≥ 4 Sterne, absteigend nach Sternen, dann zuletzt bewertet) |
| `play` | `uris`: [string] (1..n), `start_index` (default 0), `resume` (bool, default true), `from_start` (bool, default false) | `{}` |
| `control` | `cmd`: `pause` \| `resume` \| `next` \| `previous` \| `seek`; `position_ms` bei seek | `{}` |
| `rate` | `uri`, `kind`, `stars` 1..5 | `{}` — `kind` muss der tatsächlichen Art des Inhalts entsprechen, sonst `invalid`. Hörspiele werden **als Ganzes** bewertet: bei `now_playing.kind == "album_track"` bewertet die App `container_uri` mit `kind:"album"` (bzw. `"audiobook"`); `album_track` selbst ist nicht bewertbar (`invalid`). |
| `room` | `entity_id`, `action`: `toggle` \| `set_brightness` \| `set_color` \| `open` \| `close` \| `stop`; `value`: brightness `0..255` oder `[r,g,b]` | `{}` |
| `tts` | `text` (max. 200 Zeichen) | `{"url":"/api/kidshub/tts/<sha>.<ext>"}` |
| `alarm_update` | Teilobjekt von `config.alarm` (nur `enabled`, `time`, `weekdays`, `with_light`) | `{}`, danach neues `config` |
| `wake_play` | – | `{}` sobald MA die Wiedergabe gestartet hat, sonst Fehler `ma_unavailable` |

**Item**
```json
{"uri":"…","kind":"playlist"|"album"|"audiobook"|"podcast"|"episode"|"track"|"album_track"|"favorites",
 "title":"…","subtitle":"…","image_url":"/api/kidshub/img?u=…&s=256",
 "stars":null,"progress":{"position_ms":0,"duration_ms":0,"finished":false}}
```
`progress` gibt es nur bei `episode`, `audiobook` und `album` (bei Alben: Fortschritt des aktuellen Titels, Fortsetzen über `track_index`).
`album_track` = Titel/Kapitel eines Hörspiel-Albums (Kinder von `album`).

`browse`-Semantik:
- `music` ohne `parent_uri`: zuerst eine Kachel `{"kind":"favorites","uri":"kidshub://favorites/track"}`
  (nur wenn mindestens 1 Lied ≥ 4 Sterne), danach die freigegebenen Playlists.
- `music` mit `parent_uri` = Playlist: deren Tracks, bei `sort=stars` nach Sternen absteigend
  (unbewertete am Ende, sonst Originalreihenfolge).
- `audiobooks`: freigegebene Alben und Hörbücher, dazu die Favoriten-Kachel `kidshub://favorites/audiobook`.
- `podcasts` ohne `parent_uri`: freigegebene Sendungen plus Favoriten-Kachel
  `kidshub://favorites/episode`.
- `podcasts` mit `parent_uri` = Sendung: Folgen, `sort=default` → eine angefangene
  (nicht fertige, `position_ms` > 0) Folge oben, danach neu → alt. `sort=stars` → nach Sternen.
- `parent_uri` = `kidshub://favorites/...` → wie `favorites`.
- Ein `parent_uri`, das nicht freigegeben ist, ergibt den Fehler `not_allowed`.

`play`-Semantik:
- Ist `music_enabled` false und der Inhalt Musik → Fehler `not_allowed`.
- `resume=true` bei `episode` / `audiobook` / `album`: Die Integration startet an der
  gespeicherten Stelle (Track-Index + Position). `from_start=true` setzt die Stelle auf 0 zurück.
- Gespielt wird über MA auf **dem Sendspin-Player dieses Geräts**. MA ≥ 2.10 vergibt dafür eine eigene `player_id`; die Integration findet den Player über `sendspin.client_id` in dessen `output_protocols` (Fallback: Name).

## 3.4 TTS
Die Integration erzeugt Ansagen über die **in HA eingerichtete TTS-Engine** (Entität aus den
Optionen der Integration, z. B. Google Gemini TTS über „Google Generative AI“). Sie nutzt
`media-source://tts/<engine>?message=…&language=…` und
`tts.async_get_media_source_audio(hass, media_source_id) -> (ext, bytes)`.
Cache-Schlüssel: `sha256(engine|language|options-json|text)`. Gecachte Dateien liegen unter
`<config>/kidshub_tts/` und werden nie automatisch gelöscht. Erzeugt die Engine nach 15 s
nichts, folgt der Fehler `tts_failed`.

## 4. Sonnenaufgangsfarben (gemeinsam für Bildschirm und Weck-Lampe)

Lineare Interpolation in RGB zwischen den Stützstellen (`p` = Fortschritt 0..1):

| p | RGB |
|---|---|
| 0.00 | (90, 10, 5) |
| 0.35 | (200, 45, 15) |
| 0.65 | (255, 120, 30) |
| 0.85 | (255, 180, 80) |
| 1.00 | (255, 225, 160) |

Helligkeit: Bildschirm `max(night.brightness, round(p*100))` %. Lampe `round(p*255)`, nur
wenn `p > 0.02`, sonst aus.

## 5. Lautstärke

- Logische Lautstärke `L ∈ 0..100`. Effektive Systemlautstärke = 0, wenn `L == 0` oder `cap == 0`, sonst
  `clamp(round(L/100 * cap/100 * streamMax), 1, maxIndex)` mit `maxIndex = max(1, round(cap/100*streamMax))` (nie stumm bei L > 0), mit `cap = max_volume` (bzw. `alarm_max_volume`
  während der Wecker klingelt).
- Die App hält die Systemlautstärke von `STREAM_MUSIC` **immer** ≤ `cap`. Jede Änderung von
  außen (Hardware-Tasten, andere Apps) wird sofort begrenzt.
- Lautstärke-Befehle, die MA per Sendspin schickt (0..100), sind die logische Lautstärke `L`.
  Die App meldet `L` per Sendspin zurück und in `state.volume`. Der von MA angelegte
  `media_player` zeigt damit `L/100`, die Grenze bleibt dabei unsichtbar wirksam.

## 6. Wecker-Zeitachse (Gerät ist Master)

T = Weckzeit, `lead` = `lead_minutes`.
1. `T-lead` → `sunrise` (wenn `lead` > 0): `p` steigt linear von 0 auf 1 bis T.
2. `T` → `ringing`: App sendet `wake_play`. Erfolg innerhalb von 8 s → MA-Wiedergabe, sonst
   eingebauter Weckton (Schleife). Die Lautstärke-Rampe `L` steigt linear von 5 auf 100 über
   **3 Minuten**, `cap = alarm_max_volume`.
3. `T+3min` = voll hochgefahren. `T+8min` → automatisch `stopped`.
4. Stopp durch Kind (Knopf, nur wenn `button_required=false`), durch `alarm_cmd stop` oder
   Auto-Aus → `stopped`: Wiedergabe stoppen, Bildschirm bleibt hell (Tagmodus). Weck-Lampe
   bleibt an.
5. `stopped` → `off` nach 60 Minuten oder beim nächsten Wecker.

## 7. Sleep-Timer (Gerät ist Master)
Startet die Wiedergabe von `kind` ∈ {`episode`,`audiobook`,`album_track`} innerhalb von
`[window_start, window_end)` (kann über Mitternacht gehen), läuft ein Timer mit `minutes`.
Bei Ablauf: 20 s ausblenden, `control pause`, Bildschirm → `night`. Ein neuer Start im
Fenster startet einen neuen Timer. `sleep_cmd` setzt oder bricht ab.

## 8. Bildschirmmodus
- `sunrise`, wenn der Wecker in `sunrise` oder `ringing` ist.
- Sonst `night` in `[night.start, night.end)`.
- Sonst `day`.
Im Nachtmodus weckt Antippen den Bildschirm für 30 s auf (Helligkeit `day_brightness`).
Danach geht er zurück in den Nachtmodus. Im Tagmodus wird nach 2 Minuten ohne Berührung
auf 30 % der `day_brightness` gedimmt.
