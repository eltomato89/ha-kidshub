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
   - **Inhalte:** erlaubte Playlists (Musik), Alben/Hörbücher (Hörspiele) und Podcasts aus
     Music Assistant.
   - **Wecker-Extras:** Wecklicht (folgt dem Sonnenaufgang), Stopp-Taste und Weckmusik.
4. **Music Assistant:** In Music Assistant den **Sendspin**-Player-Provider aktivieren und dort
   **„Unverschlüsselte Verbindungen erlauben“** einschalten (die App verbindet sich per
   `ws://<MA-Host>:8927/sendspin`). Die App meldet sich dann als Player „KidsHub Paul“ an;
   Änderungen am Music-Assistant-Eintrag werden automatisch an die App übertragen.

Der Name „Paul“ ist nur ein Beispiel.

## Protokoll

Siehe [docs/protocol.md](docs/protocol.md).

## Lizenz

MIT
