// Run with the packaged Node from Zed's terminal so the real app sandbox applies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const process = require('node:process');
const { spawnSync } = require('node:child_process');

const shell = process.env.SHELL;
assert.ok(shell, 'SHELL must be provided by Zed');
const directory = fs.mkdtempSync(path.join(process.cwd(), 'shell-acceptance-'));
const results = [];
const output = path.resolve(process.argv[2] || 'shell-acceptance.json');

function check(name, callback) {
  try {
    callback();
    results.push({ name, passed: true });
  } catch (error) {
    results.push({ name, passed: false, error: error.message });
  }
}

function run(command, options = {}) {
  const result = spawnSync(shell, ['-c', command], {
    cwd: directory, encoding: 'utf8', timeout: 10000, ...options,
  });
  assert.equal(result.error, undefined);
  return result;
}

function succeeds(command, expected, options) {
  const result = run(command, options);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, expected);
}

try {
  for (const executable of ['/bin/sh', '/system/bin/sh']) {
    check(`absolute shell ${executable}`, () => {
      succeeds(`${executable} -c 'printf nested'`, 'nested');
    });
  }
  check('nested PATH shells', () => succeeds("sh -c \"sh -c 'printf nested'\"", 'nested'));
  check('inherited SHELL', () => succeeds('"$SHELL" -c \'printf inherited\'', 'inherited'));
  check('exit status', () => assert.equal(run('/bin/sh -c "exit 7"').status, 7));
  check('quoted arguments', () => succeeds('/bin/sh -c \'printf "%s|%s" "$1" "$2"\' sh "a b" "c*d"', 'a b|c*d'));
  check('stdin', () => succeeds('/bin/sh -c \'read value; printf "%s" "$value"\'', 'input text', { input: 'input text\n' }));
  check('environment and directory', () => succeeds('/bin/sh -c \'printf "%s|%s" "$VALUE" "$PWD"\'', `a=b\nc|${directory}`, {
    env: { ...process.env, VALUE: 'a=b\nc' },
  }));
  check('pipeline', () => succeeds('printf pipe | /bin/sh -c \'read value; printf "%s" "$value"\'', 'pipe'));
  check('background wait', () => assert.equal(run('/bin/sh -c "exit 9" & child=$!; wait "$child"').status, 9));
  check('exec preserves PID', () => {
    const result = run('printf "%s\\n" "$$"; exec /bin/sh -c \'printf "%s\\n" "$$"\'');
    assert.equal(result.status, 0, result.stderr);
    const identifiers = result.stdout.trim().split('\n');
    assert.equal(identifiers.length, 2);
    assert.match(identifiers[0], /^[0-9]+$/);
    assert.equal(identifiers[0], identifiers[1]);
  });
  check('signal delivery', () => assert.equal(run("trap 'exit 42' TERM; kill -TERM $$").status, 42));
  check('Node in PATH', () => succeeds('node -p process.version', process.version + '\n'));
  check('here document', () => succeeds('cat <<END\nhere document\nEND\n', 'here document\n'));
  check('command substitution', () => succeeds('printf "%s" "$(printf substituted)"', 'substituted'));
  check('redirection and append', () => succeeds('printf one > output; printf two >> output; cat output', 'onetwo'));
  check('nonexistent command', () => assert.equal(run('./missing-executable').status, 127));
  check('non-executable file', () => {
    fs.writeFileSync(path.join(directory, 'not-executable'), 'printf incorrectly-executed', { mode: 0o600 });
    const result = run('./not-executable');
    assert.equal(result.status, 126);
    assert.equal(result.stdout, '');
  });
  check('directory is not executable', () => {
    const result = run('/system/bin');
    assert.equal(result.status, 126);
    assert.equal(result.stdout, '');
  });
  check('shell script argument', () => {
    fs.writeFileSync(path.join(directory, 'script'), 'printf script', { mode: 0o600 });
    succeeds('sh ./script', 'script');
  });
} finally {
  fs.rmSync(directory, { recursive: true });
}
fs.writeFileSync(output, JSON.stringify({ shell, results }, null, 2) + '\n');
const failed = results.filter(result => !result.passed);
console.log(`${results.length - failed.length}/${results.length} shell checks passed`);
for (const failure of failed) console.error(failure.name + ': ' + failure.error);
process.exitCode = failed.length ? 1 : 0;
