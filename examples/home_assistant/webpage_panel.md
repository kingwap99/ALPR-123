# Add ALPR Gate to the Home Assistant sidebar

1. Open **Settings → Dashboards**.
2. Choose **Add dashboard**.
3. Select **Webpage panel**.
4. Enter:

   ```text
   http://ALPR_HOST:8099/
   ```

5. Set the title to `ALPR` and the URL path to `alpr-panel`.
6. Create the dashboard.

The page contains the live camera, last event, history, camera selector, and ROI editor.

If Home Assistant is opened through HTTPS, the browser may block an HTTP iframe. Use a local HTTP HA session or open `http://ALPR_HOST:8099/` directly.
