# SGR seat watcher

Checks the public Madaraka Express (metickets.krc.co.ke) search every ~15 minutes for one
departure and sends a WhatsApp message the moment the watched class has seats. It does not
book or pay. A daily check-in confirms it is still running; `sgr_watchdog.yml` alerts if it stops.

Train, date and class are set in `.github/workflows/sgr.yml`. Recipients and the WhatsApp
bridge credentials are repository secrets (`WA_MCP_URL`, `WA_MCP_TOKEN`, `SGR_TO_NUMBER`,
`SGR_ALERT_NUMBER`); repository variable `SGR_ARMED=1` turns sending on.
