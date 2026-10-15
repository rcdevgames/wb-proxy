// PM2 ecosystem for the WB Gateway (headless Python aiohttp service).
// The venv interpreter is required: PM2's default `node` interpreter would
// parse app.py as JavaScript. Data dir is gitignored runtime state.
module.exports = {
  apps: [
    {
      name: 'wb-gateway',
      cwd: __dirname,
      script: 'app.py',
      interpreter: __dirname + '/.venv/bin/python',
      args: '--headless --host 127.0.0.1 --port 8787 --data-dir ' + __dirname + '/data',
      exec_mode: 'fork',
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      kill_timeout: 10000,
      env: {
        WB_HOST: '127.0.0.1',
        WB_PORT: '8787',
        WB_DATA_DIR: __dirname + '/data',
        // Browser UI is served from the public host, so its same-origin POSTs
        // carry an Origin header the app must recognise.
        WB_ALLOWED_ORIGINS: 'https://via-wb.rcdevgames.net',
        // Caddy terminates TLS on the same host; trust its X-Forwarded-For.
        WB_TRUSTED_PROXY_IPS: '127.0.0.1',
        PYTHONUNBUFFERED: '1'
      }
    }
  ]
};
