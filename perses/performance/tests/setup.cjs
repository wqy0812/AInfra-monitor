// Match upstream's jsdom structuredClone shim.
if(typeof structuredClone==='undefined'){const v8=require('v8');global.structuredClone=value=>v8.deserialize(v8.serialize(value));}
