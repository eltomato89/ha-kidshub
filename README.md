# KidsHub

Echo-Show-Kinderzentrale für Home Assistant. KidsHub ist eine Home-Assistant-Integration, die der nativen Android-App KidsHub (auf einem Echo Show) kontrollierten Zugriff auf Licht, Musik, Hörspiele, Wecker und Schlaf-Timer gibt. Alle Aktionen werden gegen Zimmer- und Inhalts-Allowlists pro Gerät geprüft.

*KidsHub: Echo Show kids' control center for Home Assistant.*

## Voraussetzungen

- Home Assistant 2025.12.0 oder neuer
- Music Assistant >= 2.7 mit aktiviertem Sendspin-Provider

## Installation (HACS)

1. HACS öffnen, Menü oben rechts, **Benutzerdefinierte Repositories**.
2. `https://github.com/eltomato89/ha-kidshub` als Kategorie **Integration** hinzufügen.
3. KidsHub installieren und Home Assistant neu starten.

## Einrichtung

1. **Einstellungen anlegen:** Einstellungen → Geräte & Dienste → Integration hinzufügen →
   **KidsHub** → **Einstellungen**. TTS-Engine (eine `tts.*`-Entität),
   Sprache und optional TTS-Optionen (z. B. Stimme) wählen. Dieser Eintrag existiert nur einmal.
2. **Gerät koppeln:** Integration erneut hinzufügen → **Gerät hinzufügen**, den Namen des
   Kindes eingeben (z. B. „Paul“). Home Assistant zeigt einen 6-stelligen Code; diesen in der
   KidsHub-App auf dem Echo Show eingeben. Danach erscheint das Gerät „KidsHub Paul“.
3. **Optionen des Geräts** (Gerät → Konfigurieren):
   - **Zimmer:** Lichter, Schalter, Rollos usw., die das Kind bedienen darf.
   - **Inhalte:** erlaubte Playlists (Musik), Alben/Hörbücher (Hörspiele), Podcasts und
     Radiosender aus Music Assistant. Radiosender müssen dafür in der MA-Bibliothek sein
     (MA: Suche → Radio → „Zur Bibliothek hinzufügen“). Radio zählt wie Musik: der Schalter
     „Musik erlaubt“ sperrt auch Radio.
   - **Zimmer → Max. Helligkeit:** optional eine Höchsthelligkeit pro Lampe (leer/100 % = keine
     Grenze). Regler, Farbe und Einschalten bleiben darunter.
   - **Wecker-Extras:** Wecklicht (folgt dem Sonnenaufgang), Stopp-Taste und Weckmusik.
4. **Music Assistant:** In Music Assistant den **Sendspin**-Player-Provider aktivieren und dort
   **„Unverschlüsselte Verbindungen erlauben“** einschalten (die App verbindet sich per
   `ws://<MA-Host>:8927/sendspin`). Die App meldet sich dann als Player „KidsHub Paul“ an;
   Änderungen am Music-Assistant-Eintrag werden automatisch an die App übertragen.

Der Name „Paul“ ist nur ein Beispiel.

## Funktionen ab 0.1.6

- **Vorlese-Knöpfe** (Schalter): blendet alle 🔊-Knöpfe der App aus.
- **Kachel Timer** (Schalter): Countdown und Stoppuhr in der App (nachts lautlos).
- **Warteschlange:** Das Kind kann Lieder, Playlists, Hörspiele und Folgen „in die
  Warteschlange“ legen. Endet die Wiedergabe von selbst, startet KidsHub den nächsten Eintrag
  (nicht nach Wecker, Pause, Sleep-Timer oder Musik-Sperre). Die Liste gehört KidsHub (nicht
  der Music-Assistant-Queue), wird jeden Morgen zum Ende der Nachtzeit geleert, die maximale
  Länge stellt **Warteschlange max. Länge** ein.
- **Weckmusik vom Kind:** Bei sichtbaren Einstellungen kann das Kind ein Lied oder eine
  Playlist als Weckmusik wählen; sie hat Vorrang vor der Eltern-Weckmusik, solange sie
  freigegeben ist. Sensor **Weckmusik**, Knopf **Weckmusik zurücksetzen**.
- **Durchsagen:** `notify.send_message` an `notify.kidshub_<kind>_durchsage` oder die Aktion
  `kidshub.announce` (mehrere Geräte, optional Lautstärke, höchstens die maximale Lautstärke)
  liest einen Text (bis 500 Zeichen) auf dem Echo vor – auch nachts. Ist das Gerät nicht
  verbunden, schlägt die Aktion mit einem Fehler fehl.

## Protokoll

Siehe [docs/protocol.md](docs/protocol.md).

## Lizenz

MIT
