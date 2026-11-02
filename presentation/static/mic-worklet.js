import {PcmResampler} from './audio-resampler.js';

class PresenceMicrophone extends AudioWorkletProcessor {
  constructor(){
    super();this.resampler=new PcmResampler(sampleRate);this.enabled=true;
    this.port.onmessage=event=>{this.enabled=event.data.enabled;this.resampler.reset();};
  }
  process(inputs,outputs){
    // This graph never passes captured sound to the loudspeakers.
    for(const output of outputs)for(const channel of output)channel.fill(0);
    if(this.enabled&&inputs[0]?.[0])this.resampler.push(inputs[0][0],(pcm,level)=>this.port.postMessage({pcm,level},[pcm]));
    return true;
  }
}
registerProcessor('presence-microphone',PresenceMicrophone);
