// Stateful windowed-sinc low-pass resampler. No frame-local decimation.
export class PcmResampler {
  constructor(inputRate, outputRate=16000, frameSamples=320) {
    if(inputRate<16000||inputRate>192000)throw new Error('不支持的音频采样率');
    this.ratio=inputRate/outputRate;this.frameSamples=frameSamples;
    this.radius=32;this.phases=256;this.ring=new Float32Array(8192);
    const cutoff=.45*Math.min(1,outputRate/inputRate);
    this.filters=Array.from({length:this.phases},(_,phase)=>{
      const coefficients=new Float32Array(this.radius*2);let sum=0;
      for(let j=0;j<coefficients.length;j++){
        const distance=j-this.radius+1-phase/this.phases;
        const sinc=Math.abs(distance)<1e-10?2*cutoff:Math.sin(2*Math.PI*cutoff*distance)/(Math.PI*distance);
        const window=.42+.5*Math.cos(Math.PI*distance/this.radius)+.08*Math.cos(2*Math.PI*distance/this.radius);
        coefficients[j]=sinc*window;sum+=coefficients[j];
      }
      for(let j=0;j<coefficients.length;j++)coefficients[j]/=sum;
      return coefficients;
    });
    this.reset();
  }
  reset(){this.total=0;this.position=0;this.used=0;this.level=0;this.frame=new Int16Array(this.frameSamples);this.ring.fill(0);}
  push(input,emit){
    for(const sample of input){
      this.ring[this.total%this.ring.length]=sample;this.total++;
      while(this.position+this.radius<this.total){
        const base=Math.floor(this.position),phase=Math.min(this.phases-1,Math.floor((this.position-base)*this.phases));
        const coefficients=this.filters[phase];let value=0;
        for(let j=0;j<coefficients.length;j++){
          const at=base+j-this.radius+1;
          if(at>=0)value+=this.ring[at%this.ring.length]*coefficients[j];
        }
        value=Math.max(-1,Math.min(1,value));this.level+=value*value;
        this.frame[this.used++]=Math.round(value*(value<0?32768:32767));
        this.position+=this.ratio;
        if(this.used===this.frameSamples){
          // DataView makes the wire contract explicitly little-endian.
          const pcm=new ArrayBuffer(this.frameSamples*2),view=new DataView(pcm);
          for(let j=0;j<this.frameSamples;j++)view.setInt16(j*2,this.frame[j],true);
          emit(pcm,Math.sqrt(this.level/this.frameSamples));this.used=0;this.level=0;
        }
      }
    }
  }
}
