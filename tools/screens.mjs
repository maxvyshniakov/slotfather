const terminal={
 source:'terminal',command:['python3','tools/dashboard-demo.py'],columns:156,rows:43,
 env:{NO_COLOR:''},
 fontSize:20,lineHeight:1.2,fontFamily:'Screen Kit Mono,JetBrains Mono,Noto Sans Symbols 2',
};
export default {cwd:'..',recipes:{
 dashboard:{...terminal,format:'png',duration:1},
 'dashboard-navigation':{...terminal,format:'webp',fps:4,duration:10.5,steps:[
  {at:.4,input:'j',expect:'label      lane-465-state-definitions-review-fixes',settle:.15},
  {at:.8,input:'j',expect:'label      desktop',settle:.15},
  {at:5.8,input:'j',expect:'waiting · #1 · wants 6',settle:.15},
  {at:8.8,input:'k',expect:'label      desktop',settle:.1},
  {at:9.05,input:'k',expect:'label      lane-465-state-definitions-review-fixes',settle:.1},
  {at:9.3,input:'k',expect:'label      lane-644-build-once',settle:.1},
 ]},
}};
