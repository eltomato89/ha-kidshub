# KidsHub

Echo-Show-Kinderzentrale für Home Assistant. KidsHub ist eine Home-Assistant-Integration, die einer Kinder-Web-App (z. B. auf einem Echo Show) kontrollierten Zugriff auf Licht, Musik, Wecker und Schlaf-Timer gibt. Alle Aktionen werden gegen Zimmer- und Inhalts-Allowlists pro Gerät geprüft.

*KidsHub: Echo Show kids' control center for Home Assistant.*

## Voraussetzungen

- Home Assistant 2025.12.0 oder neuer
- Music Assistant >= 2.7 mit aktiviertem Sendspin-Provider

## Installation (HACS)

1. HACS öffnen, Menü oben rechts, **Benutzerdefinierte Repositories**.
2. `https://github.com/eltomato89/ha-kidshub` als Kategorie **Integration** hinzufügen.
3. KidsHub installieren und Home Assistant neu starten.
4. Unter Einstellungen, Geräte & Dienste die Integration **KidsHub** hinzufügen.

## Protokoll

Siehe [docs/protocol.md](docs/protocol.md).

## Lizenz

MIT
