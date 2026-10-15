// Adapt logical browser characters to the guest's physical keymap. QEMU's
// extended VNC protocol transmits scan codes, not just the Unicode keysym.
export const layouts={fr:'Français — AZERTY',us:'Anglais US — QWERTY',gb:'Anglais UK — QWERTY',de:'Allemand — QWERTZ',native:'Touches physiques (sans adaptation)'};
const us={};
for(const c of 'abcdefghijklmnopqrstuvwxyz')us['Key'+c.toUpperCase()]=[c,c.toUpperCase()];
for(let i=0;i<10;i++)us['Digit'+i]=[String(i),')!@#$%^&*('[i]];
Object.assign(us,{Backquote:['`','~'],Minus:['-','_'],Equal:['=','+'],BracketLeft:['[','{'],BracketRight:[']','}'],Backslash:['\\','|'],Semicolon:[';',':'],Quote:["'",'"'],Comma:[',','<'],Period:['.','>'],Slash:['/','?'],Space:[' ']});
const fr={...us,KeyA:['q','Q'],KeyQ:['a','A'],KeyW:['z','Z'],KeyZ:['w','W'],KeyM:[',','?'],KeyE:['e','E','€'],Semicolon:['m','M'],Backquote:['²'],Minus:[')','°',']'],Equal:['=','+','}'],BracketLeft:['^','¨'],BracketRight:['$','£','¤'],Backslash:['*','µ'],Quote:['ù','%'],IntlBackslash:['<','>'],Comma:[';','.'],Period:[':','/'],Slash:['!','§']};
const frDigits=[['à','0','@'],['&','1'],['é','2','~'],['"','3','#'],["'",'4','{'],['(','5','['],['-','6','|'],['è','7','`'],['_','8','\\'],['ç','9','^']];
frDigits.forEach((row,i)=>fr['Digit'+i]=row);
const gb={...us,Digit2:['2','"'],Digit3:['3','£'],Quote:["'",'@'],Backquote:['`','¬'],Backslash:['#','~'],IntlBackslash:['\\','|']};
const de={...us,KeyY:['z','Z'],KeyZ:['y','Y'],KeyQ:['q','Q','@'],KeyE:['e','E','€'],KeyM:['m','M','µ'],Digit2:['2','"','²'],Digit3:['3','§','³'],Digit6:['6','&'],Digit7:['7','/','{'],Digit8:['8','(','['],Digit9:['9',')',']'],Digit0:['0','=','}'],Minus:['ß','?','\\'],Equal:['´','`'],BracketLeft:['ü','Ü'],BracketRight:['+','*','~'],Backslash:['#',"'"],Semicolon:['ö','Ö'],Quote:['ä','Ä'],Comma:[',',';'],Period:['.',':'],Slash:['-','_'],Backquote:['^','°'],IntlBackslash:['<','>','|']};
const tables={fr,us,gb,de};
const indexes={};
for(const [name,table] of Object.entries(tables)){
    const index=new Map();
    // Prefer ordinary keys over AltGr alternatives when both produce a symbol.
    for(let level=0;level<3;level++)for(const [code,chars] of Object.entries(table))if(chars[level]&&!index.has(chars[level]))index.set(chars[level],{code,shift:level===1,alt:level===2});
    indexes[name]=index;
}
const modifiers={ShiftLeft:0xffe1,ShiftRight:0xffe2,AltRight:0xffea};
export class LayoutKeyboard{
    constructor(rfb,layout='fr'){
        this.rfb=rfb;this.layout=layout;this.held=new Set();this.pressed=new Map();this.capsLock=false;
        this.original=rfb.sendKey;this.raw=rfb.sendKey.bind(rfb);this.send=this.send.bind(this);rfb.sendKey=this.send;
    }
    setLayout(layout){if(!Object.hasOwn(layouts,layout))return;this.release();this.layout=layout;}
    release(){for(const [source,key] of this.pressed)this.raw(key.symbol,key.code,false);this.pressed.clear();for(const code of this.held)this.raw(modifiers[code],code,false);this.held.clear();}
    destroy(){this.release();if(this.rfb.sendKey===this.send)this.rfb.sendKey=this.original;}
    send(symbol,code,down){
        if(down===undefined){this.send(symbol,code,true);this.send(symbol,code,false);return;}
        if(Object.hasOwn(modifiers,code)){if(down)this.held.add(code);else this.held.delete(code);this.raw(symbol,code,down);return;}
        if(!down){const key=this.pressed.get(code);this.raw(key?.symbol??symbol,key?.code??code,false);this.pressed.delete(code);return;}
        const point=(symbol&0xff000000)===0x01000000?symbol&0xffffff:symbol;
        const char=point>=32&&point<0xff00?String.fromCodePoint(point):null;
        const target=this.layout==='native'?null:indexes[this.layout]?.get(char);
        if(!target){this.raw(symbol,code,true);this.pressed.set(code,{symbol,code});return;}
        // Caps Lock changes alphabetic keys only; preserve browser/guest sync.
        const letter=char.toLowerCase()!==char.toUpperCase();
        const shift=letter&&this.capsLock?!target.shift:target.shift;
        const changes=[];
        const change=(key,wanted)=>{const active=this.held.has(key);if(active!==wanted){this.raw(modifiers[key],key,wanted);changes.push([key,active]);}};
        if(shift){if(!this.held.has('ShiftLeft')&&!this.held.has('ShiftRight'))change('ShiftLeft',true);}
        else {change('ShiftLeft',false);change('ShiftRight',false);}
        change('AltRight',target.alt);
        this.raw(symbol,target.code,true);this.pressed.set(code,{symbol,code:target.code});
        for(const [key,active] of changes.reverse())this.raw(modifiers[key],key,active);
    }
}
