// Rebuild only patched files from audited upstream sources. No registry writes.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const root = path.resolve(process.argv[2]);
const patchRoot = __dirname;
const lock = JSON.parse(fs.readFileSync(path.join(patchRoot, 'source-lock.json')));
const swc = require(path.join(root, 'ui/node_modules/@swc/core'));
for (const [name, entry] of Object.entries(lock.files)) {
  const map = path.join(root, entry.target + '.js.map');
  const digest = crypto.createHash('sha256').update(fs.readFileSync(map)).digest('hex');
  if (digest !== entry.original_map_sha256) throw new Error(`Upstream source mismatch: ${name}`);
}
for (const [name, entry] of Object.entries(lock.files)) {
  const src = fs.readFileSync(path.join(patchRoot, 'patches', name), 'utf8');
  for (const type of ['es6', 'commonjs']) {
    const target = path.join(root, type === 'es6' ? entry.target + '.js' : entry.target.replace('/dist/', '/dist/cjs/') + '.js');
    const result = swc.transformSync(src, {filename:name, sourceMaps:false, jsc:{target:'es2022',parser:{syntax:'typescript',tsx:name.endsWith('.tsx')},transform:{react:{runtime:'automatic'}}},module:{type}});
    fs.writeFileSync(target, result.code);
  }
}
// Keep declaration consumers compatible with the added optional loading API.
const model = path.join(root, 'ui/node_modules/@perses-dev/plugin-system/dist/model/plugin-loading.d.ts');
if (!fs.readFileSync(model,'utf8').includes('importPlugin?:')) fs.writeFileSync(model, fs.readFileSync(model,'utf8').replace('export interface PluginLoader {', 'export interface PluginLoader {\n    importPlugin?: (resource: PluginModuleResource, name: string) => Promise<unknown>;'));
const hook = path.join(root, 'ui/node_modules/@perses-dev/plugin-system/dist/runtime/plugin-registry.d.ts');
fs.writeFileSync(hook, fs.readFileSync(hook,'utf8').replace('usePluginBuiltinVariableDefinitions()', 'usePluginBuiltinVariableDefinitions(requiredPluginNames?: string[])'));
if (!fs.readFileSync(hook,'utf8').includes('retryFailedPluginQueries')) fs.appendFileSync(hook, '\nexport declare function retryFailedPluginQueries(queryClient: import("@tanstack/react-query").QueryClient): Promise<void>;\n');
console.log(`Applied ${Object.keys(lock.files).length} source patches`);
