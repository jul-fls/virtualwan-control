import assert from 'node:assert/strict';
import {LayoutKeyboard} from '../virtualwan/static/keyboard-layout.js';
const sent=[],rfb={sendKey:(...args)=>sent.push(args)};const original=rfb.sendKey;
const keyboard=new LayoutKeyboard(rfb,'us');
// Browser AZERTY "a" lives at physical KeyQ; the QWERTY guest needs KeyA.
rfb.sendKey(97,'KeyQ',true);rfb.sendKey(97,'KeyQ',false);
assert.deepEqual(sent.splice(0),[[97,'KeyA',true],[97,'KeyA',false]]);
keyboard.setLayout('fr');rfb.sendKey(97,'KeyA',true);rfb.sendKey(97,'KeyA',false);
assert.deepEqual(sent.splice(0),[[97,'KeyQ',true],[97,'KeyQ',false]]);
// Digits, punctuation, accented letters and AltGr require different levels.
rfb.sendKey(49,'Digit1');assert.deepEqual(sent.splice(0),[[65505,'ShiftLeft',true],[49,'Digit1',true],[65505,'ShiftLeft',false],[49,'Digit1',false]]);
rfb.sendKey(233,'Quote');assert.deepEqual(sent.splice(0),[[233,'Digit2',true],[233,'Digit2',false]]);
rfb.sendKey(64,'Digit2');assert.deepEqual(sent.splice(0),[[65514,'AltRight',true],[64,'Digit0',true],[65514,'AltRight',false],[64,'Digit0',false]]);
rfb.sendKey(109,'KeyM');assert.deepEqual(sent.splice(0),[[109,'Semicolon',true],[109,'Semicolon',false]]);
// Releasing Shift before a letter must not lose the translated key-up.
rfb.sendKey(65505,'ShiftLeft',true);rfb.sendKey(65,'KeyA',true);rfb.sendKey(65505,'ShiftLeft',false);rfb.sendKey(97,'KeyA',false);
assert.deepEqual(sent.splice(0),[[65505,'ShiftLeft',true],[65,'KeyQ',true],[65505,'ShiftLeft',false],[65,'KeyQ',false]]);
keyboard.setLayout('de');rfb.sendKey(122,'KeyZ');assert.deepEqual(sent.splice(0),[[122,'KeyY',true],[122,'KeyY',false]]);
keyboard.setLayout('gb');rfb.sendKey(64,'Digit2');assert.deepEqual(sent.splice(0),[[65505,'ShiftLeft',true],[64,'Quote',true],[65505,'ShiftLeft',false],[64,'Quote',false]]);
keyboard.setLayout('native');rfb.sendKey(97,'KeyQ',true);keyboard.setLayout('us');assert.deepEqual(sent.splice(0),[[97,'KeyQ',true],[97,'KeyQ',false]]);
keyboard.setLayout('fr');keyboard.capsLock=true;rfb.sendKey(65,'KeyA');assert.deepEqual(sent.splice(0),[[65,'KeyQ',true],[65,'KeyQ',false]]);
keyboard.destroy();assert.equal(rfb.sendKey,original);
console.log('Keyboard layouts: AZERTY/QWERTY/QWERTZ, modifiers, key-up, persistence boundaries OK');
