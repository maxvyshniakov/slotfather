const terminal={
 source:'terminal',command:['python3','tools/dashboard-demo.py'],columns:176,rows:50,
 env:{NO_COLOR:''},
 fontSize:18,lineHeight:1.2,fontFamily:'Screen Kit Mono,JetBrains Mono,Noto Sans Symbols 2',
};
export default {cwd:'..',recipes:{
 dashboard:{...terminal,format:'png',duration:1,at:.3},
 'dashboard-navigation':{...terminal,format:'webp',fps:4,duration:11.5,steps:[
  {at:.4,input:'j',expect:['label      lane-465-state-definitions-review-fixes','stage      active'],settle:.25},
  {at:2,expect:['label      lane-465-state-definitions-review-fixes','ticket     #465','stage      active'],settle:.25},
  {at:3.7,input:'j',expect:'label      desktop',settle:.25},
  {at:4,input:'j',expect:'waiting · #1 · wants 6',settle:.25},
  {at:4.3,input:'j',expect:['waiting · #2 · wants 1','ticket     #555','stage      active'],settle:.25},
  {at:6.8,input:'\x1b[<65;150;12M'.repeat(4),expect:['above','claims'],settle:.25},
  {at:8.3,input:'g',expect:['label      lane-644-build-once','receipt    in progress'],settle:.25},
  {at:9,input:'j',expect:'label      lane-465-state-definitions-review-fixes',settle:.25},
  {at:9.4,input:'\x1b[<65;150;12M'.repeat(6),expect:['above','claims'],settle:.25},
  {at:10.4,input:'g',expect:['label      lane-644-build-once','stage      active'],settle:.25},
 ]},
}};
