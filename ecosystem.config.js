// pm2 start ecosystem.config.js && pm2 save
module.exports = {
  apps: [
    {
      name: "sasta-flight",
      cwd: __dirname,
      script: ".venv/bin/python",
      args: "-m bot.main",
      interpreter: "none",
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      // A crash loop would also hammer Google; back off between restarts.
      exp_backoff_restart_delay: 5000,
      max_restarts: 20,
      kill_timeout: 15000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
