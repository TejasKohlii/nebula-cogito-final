'use strict';

function validateConnection({ provider, apiKey }) {
  const invalid = (message) => Object.assign(new Error(message), { status: 400 });
  if (provider !== 'cli' && provider !== 'api') {
    throw invalid('Choose Claude Code CLI or Anthropic API before optimizing.');
  }
  if (provider === 'api' && (typeof apiKey !== 'string' || !apiKey.trim()
      || apiKey.length > 4096 || !/^[\x21-\x7e]+$/.test(apiKey.trim()))) {
    throw invalid('Enter a valid Anthropic API key.');
  }
}

function connectionEnvironment(connection, base) {
  validateConnection(connection);
  const env = { ...base, COGITO_PROVIDER: connection.provider };
  // Credentials stay in this run's environment, never the shared server
  // environment, command line, task config, status response or saved logs.
  for (const name of ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN']) delete env[name];
  if (connection.provider === 'api') {
    env.ANTHROPIC_API_KEY = connection.apiKey.trim();
    // A key entered for Anthropic must go to the direct Anthropic endpoint.
    for (const name of ['ANTHROPIC_BASE_URL', 'ANTHROPIC_CUSTOM_HEADERS',
      'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_BIN', 'SHINKA_HEADLESS_COMMAND']) delete env[name];
  }
  return env;
}

module.exports = { validateConnection, connectionEnvironment };
