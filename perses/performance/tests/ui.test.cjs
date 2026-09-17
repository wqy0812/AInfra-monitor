const React = require('react');
const { render, act, waitFor, cleanup } = require('@testing-library/react');
const { QueryClient, QueryClientProvider, useQuery } = require('@tanstack/react-query');
const path = require('path');
const base = path.join(process.env.PERSES_SOURCE, 'ui/node_modules/@perses-dev/plugin-system/dist/cjs');
const { TimeRangeProvider, useTimeRange } = require(path.join(base,'runtime/TimeRangeProvider/TimeRangeProvider'));
const { PluginRegistry } = require(path.join(base,'components/PluginRegistry/PluginRegistry'));
const { usePluginRegistry, usePluginBuiltinVariableDefinitions } = require(path.join(base,'runtime/plugin-registry'));
let ctx;
function Probe({calls}) {
 ctx = useTimeRange();
 useQuery({queryKey:['query',ctx.absoluteTimeRange],queryFn:async()=>{calls.push(+ctx.absoluteTimeRange.end);return 1},staleTime:Infinity});
 return null;
}
function fixture(range, refreshInterval='0s') {
 const calls=[]; const client = new QueryClient({defaultOptions:{queries:{retry:false}}});
 const ui=render(React.createElement(QueryClientProvider,{client},React.createElement(TimeRangeProvider,{timeRange:range,refreshInterval,setTimeRange:()=>{},setRefreshInterval:()=>{}},React.createElement(Probe,{calls}))));
 return {calls,client,ui};
}
afterEach(()=>{cleanup();jest.useRealTimers();jest.restoreAllMocks()});
test('relative manual refresh executes only the new range',async()=>{
 const x=fixture({pastDuration:'1h'}); await waitFor(()=>expect(x.calls.length).toBe(1));
 const old=x.calls[0]; await new Promise(r=>setTimeout(r,10)); await act(async()=>ctx.refresh());
 await waitFor(()=>expect(x.calls.length).toBe(2));expect(x.calls[1]).toBeGreaterThan(old);
 await new Promise(r=>setTimeout(r,20));expect(x.calls).toHaveLength(2);expect(x.client.getQueryCache().getAll().length).toBe(1);x.client.clear();
});
test('fixed range refresh still refetches once',async()=>{
 const x=fixture({start:new Date('2026-01-01'),end:new Date('2026-01-02')});await waitFor(()=>expect(x.calls.length).toBe(1));await act(async()=>ctx.refresh());await waitFor(()=>expect(x.calls.length).toBe(2));expect(x.calls[0]).toBe(x.calls[1]);x.client.clear();
});
test('auto refresh fires once per tick and cleans up on unmount',async()=>{
 jest.useFakeTimers();const x=fixture({pastDuration:'1h'},'15s');await act(async()=>{});expect(x.calls).toHaveLength(1);
 for(let i=0;i<3;i++){await act(async()=>{jest.advanceTimersByTime(15000)});await act(async()=>{jest.advanceTimersByTime(1)});expect(x.calls).toHaveLength(i+2)}
 x.ui.unmount();await act(async()=>{jest.advanceTimersByTime(30000)});expect(x.calls).toHaveLength(4);x.client.clear();
});
const modules=[{kind:'PluginModule',metadata:{name:'Prometheus',version:'0.58.0',registry:'perses.dev'},spec:{plugins:[{kind:'Datasource',spec:{name:'PrometheusDatasource'}},{kind:'TimeSeriesQuery',spec:{name:'PrometheusTimeSeriesQuery'}}]}},{kind:'PluginModule',metadata:{name:'Other',version:'1',registry:'perses.dev'},spec:{plugins:[{kind:'Datasource',spec:{name:'OtherDatasource'}}]}}];
test('builtin catalog loads only modules used by the dashboard; single plugin imports deduplicate',async()=>{
 const calls=[];const client=new QueryClient({defaultOptions:{queries:{retry:false}}});let registry,values;
 function P(){registry=usePluginRegistry();values=usePluginBuiltinVariableDefinitions(['PrometheusTimeSeriesQuery']).data;return null}
 const {getPluginModuleCompoundKey}=require(path.join(base,'model/plugins'));
 const loader={getInstalledPlugins:async()=>modules,importPluginModule:async()=>{throw Error('should use single import')},importPlugin:async(r,name)=>{calls.push(name);const p=r.spec.plugins.find(p=>p.spec.name===name);return {[getPluginModuleCompoundKey({kind:p.kind,name,version:r.metadata.version,registry:r.metadata.registry})]:{getBuiltinVariableDefinitions:()=>[{spec:{name:'__interval'}}]}}}};
 render(React.createElement(QueryClientProvider,{client},React.createElement(PluginRegistry,{pluginLoader:loader},React.createElement(P))));await waitFor(()=>expect(values).toHaveLength(1));expect(calls).toEqual(['PrometheusDatasource']);
 await Promise.all([registry.getPlugin({kind:'TimeSeriesQuery',name:'PrometheusTimeSeriesQuery'}),registry.getPlugin({kind:'TimeSeriesQuery',name:'PrometheusTimeSeriesQuery'})]);expect(calls).toEqual(['PrometheusDatasource','PrometheusTimeSeriesQuery']);client.clear();
});
test('late calculated builtin does not duplicate queries; real variable change does',async()=>{
 const {useTimeSeriesQueries}=require(path.join(base,'runtime/time-series-queries'));
 const {PluginRegistryContext}=require(path.join(base,'runtime/plugin-registry'));
 const {VariableContext}=require(path.join(base,'runtime/variables'));
 const {BuiltinVariableContext}=require(path.join(base,'runtime/builtin-variables'));
 const {DatasourceStoreContext}=require(path.join(base,'runtime/datasources'));
 const calls=[];const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 const plugin={dependsOn:()=>({variables:['role','__interval']}),getTimeSeriesData:async(s,ctx)=>{calls.push(ctx.variableState.role.value);return {series:[]}}};
 const registry={getPlugin:async()=>plugin};const range={start:new Date('2026-01-01'),end:new Date('2026-01-02')};
 function P(){useTimeSeriesQueries([{kind:'TimeSeriesQuery',spec:{plugin:{kind:'PrometheusTimeSeriesQuery',spec:{query:'foo[$__interval]'}}}}],{suggestedStepMs:5000});return null}
 function tree(role,builtins){return React.createElement(QueryClientProvider,{client},React.createElement(TimeRangeProvider,{timeRange:range,refreshInterval:'0s',setTimeRange:()=>{},setRefreshInterval:()=>{}},React.createElement(PluginRegistryContext.Provider,{value:registry},React.createElement(VariableContext.Provider,{value:{state:role?{role:{value:role,loading:false}}:{}}},React.createElement(BuiltinVariableContext.Provider,{value:{variables:builtins}},React.createElement(DatasourceStoreContext.Provider,{value:{}},React.createElement(P)))))))}
 const view=render(tree(null,[]));await act(async()=>{});expect(calls).toHaveLength(0);
 view.rerender(tree('decode',[]));await waitFor(()=>expect(calls).toEqual(['decode']));
 view.rerender(tree('decode',[{spec:{name:'__interval',value:()=>'$__interval'}}]));await act(async()=>{});expect(calls).toEqual(['decode']);
 view.rerender(tree('prefill',[]));await waitFor(()=>expect(calls).toEqual(['decode','prefill']));client.clear();
});
test('failed single-plugin import can be retried without reloading successful modules',async()=>{
 const {getPluginModuleCompoundKey}=require(path.join(base,'model/plugins'));let registry;let attempts=0;
 function P(){registry=usePluginRegistry();return null}
 const loader={getInstalledPlugins:async()=>modules,importPluginModule:async()=>{},importPlugin:async(r,name)=>{attempts++;if(attempts===1)throw Error('test failure');return {[getPluginModuleCompoundKey({kind:'Datasource',name,version:r.metadata.version,registry:r.metadata.registry})]:{}}}};
 render(React.createElement(PluginRegistry,{pluginLoader:loader},React.createElement(P)));
 let failure;try{await registry.getPlugin({kind:'Datasource',name:'PrometheusDatasource'})}catch(error){failure=error.message}expect(failure).toBe('test failure');
 await registry.getPlugin({kind:'Datasource',name:'PrometheusDatasource'});await registry.getPlugin({kind:'Datasource',name:'PrometheusDatasource'});expect(attempts).toBe(2);
});
test('retry button restarts failed active plugin queries and preserves successful plugins',async()=>{
 const originalError=console.error;jest.spyOn(console,'error').mockImplementation((error,...rest)=>{if(error?.message!=='temporary plugin failure')originalError(error,...rest)});
 const {retryFailedPluginQueries}=require(path.join(base,'runtime/plugin-registry'));
 const client=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});let offline=true,failedCalls=0,successfulCalls=0;
 function P(){
  useQuery({queryKey:['getPlugin','TimeSeriesQuery','PrometheusTimeSeriesQuery'],queryFn:async()=>{failedCalls++;if(offline)throw Error('temporary plugin failure');return 'recovered'}});
  useQuery({queryKey:['getPlugin','Panel','TimeSeriesChart'],queryFn:async()=>{successfulCalls++;return 'chart'}});
  return null;
 }
 render(React.createElement(QueryClientProvider,{client},React.createElement(P)));
 const key=['getPlugin','TimeSeriesQuery','PrometheusTimeSeriesQuery'];
 await waitFor(()=>expect(client.getQueryState(key).status).toBe('error'));
 offline=false;await act(async()=>{await retryFailedPluginQueries(client)});
 await waitFor(()=>expect(client.getQueryData(key)).toBe('recovered'));
 expect(failedCalls).toBe(2);expect(successfulCalls).toBe(1);client.clear();
});
