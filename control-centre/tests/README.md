The browser suite serves `control-centre/src` and intercepts every `/api/v1/`
request. It never connects to ERPNext or Tally and uses no saved credentials.

Use Node.js 20 or newer. From the repository root:

```sh
npm --prefix control-centre ci
npm --prefix control-centre exec -- playwright install chromium
npm --prefix control-centre run test:ui
```

Chromium must have its normal operating-system libraries available. Set
`PLAYWRIGHT_BROWSERS_PATH` when using a browser cache outside the default
location. `npm run test:ui -- --headed` runs the same suite interactively from
`control-centre`.

Tests cover navigation, selection persistence, form validation, async errors,
master results, flow preview and configuration, sync-aware health checks, and
mobile layout. Failure traces and screenshots are written to the ignored
`test-results` directory.
