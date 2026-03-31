import fs from 'node:fs';

export function validateConfig(config) {
  if (!config.machines || !Array.isArray(config.machines)) {
    throw new Error('Config must have a "machines" array');
  }
  if (config.machines.length === 0) {
    throw new Error('Config must have at least one machine');
  }

  const names = new Set();
  for (const m of config.machines) {
    if (!m.name) throw new Error('Each machine must have a "name"');
    if (!m.socket) throw new Error(`Machine "${m.name}" must have a "socket" path`);
    if (names.has(m.name)) throw new Error(`Duplicate machine name: "${m.name}"`);
    names.add(m.name);

    if (m.local === false && !m.ssh_tunnel) {
      throw new Error(`Remote machine "${m.name}" must have "ssh_tunnel" config`);
    }
  }

  return config;
}

export function loadConfig(filePath) {
  const raw = fs.readFileSync(filePath, 'utf-8');
  const config = JSON.parse(raw);
  return validateConfig(config);
}
