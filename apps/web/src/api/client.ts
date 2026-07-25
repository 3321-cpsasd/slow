const BASE = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000';

async function call<T>(path:string, init?:RequestInit):Promise<T>{
  const response = await fetch(`${BASE}${path}`, {headers:{'Content-Type':'application/json'}, ...init});
  const text = await response.text();
  const payload = text ? JSON.parse(text) : undefined;
  if(!response.ok) throw new Error(payload.error || '请求失败');
  return payload as T;
}

async function streamQa(
  path:string,
  body:object,
  onDelta:(delta:string)=>void,
):Promise<{sessionId:string;threadId:string;relation:string}>{
  const response = await fetch(`${BASE}${path}`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body),
  });
  if(!response.ok){
    const text = await response.text();
    const payload = text ? JSON.parse(text) : undefined;
    throw new Error(payload?.error || '答疑发送失败');
  }
  if(!response.body) throw new Error('浏览器不支持流式答疑');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed:{sessionId:string;threadId:string;relation:string}|undefined;
  while(true){
    const {value,done} = await reader.read();
    buffer += decoder.decode(value,{stream:!done});
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for(const line of lines){
      if(!line.trim()) continue;
      const event = JSON.parse(line);
      if(event.type === 'delta') onDelta(event.delta);
      if(event.type === 'done') completed = event;
      if(event.type === 'error') throw new Error(event.error || '答疑生成失败');
    }
    if(done) break;
  }
  if(!completed) throw new Error('答疑流意外结束');
  return completed;
}

export const api = {
  bootstrap:()=>call<import('../model/types').Bootstrap>('/api/bootstrap'),
  createPlan:(body:object,idempotencyKey:string)=>call<import('../model/types').Series>('/api/plans',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':idempotencyKey},body:JSON.stringify(body)}),
  series:(id:string)=>call<import('../model/types').Series>(`/api/series/${id}`),
  deleteSeries:(id:string)=>call<void>(`/api/series/${id}`,{method:'DELETE'}),
  chapter:(id:string)=>call<import('../model/types').Chapter>(`/api/chapters/${id}/generate`,{method:'POST'}),
  section:(id:string)=>call<import('../model/types').Section>(`/api/sections/${id}`),
  generateSection:(id:string)=>call<import('../model/types').Section>(`/api/sections/${id}/generate`,{method:'POST'}),
  quiz:(id:string,quizSetId:string,answers:number[][])=>call<any>(`/api/sections/${id}/quiz`,{method:'POST',body:JSON.stringify({quizSetId,answers})}),
  ask:(id:string,blockId:string,question:string,threadId?:string,forceRelation?:'follow_up'|'new_question')=>call<any>(`/api/sections/${id}/ask`,{method:'POST',body:JSON.stringify({blockId,question,threadId,forceRelation})}),
  askStream:(id:string,blockId:string,question:string,onDelta:(delta:string)=>void,threadId?:string,forceRelation?:'follow_up'|'new_question')=>streamQa(`/api/sections/${id}/ask/stream`,{blockId,question,threadId,forceRelation},onDelta),
  correctQa:(id:string,threadId:string,targetThreadId:string)=>call<any>(`/api/sections/${id}/qa/threads/${threadId}`,{method:'PATCH',body:JSON.stringify({relation:'follow_up',targetThreadId})}),
  askMe:(id:string,answer='')=>call<import('../model/types').AskMe>(`/api/sections/${id}/ask-me`,{method:'POST',body:JSON.stringify({answer})}),
  note:(id:string,content:object)=>call<any>(`/api/sections/${id}/note`,{method:'PATCH',body:JSON.stringify({content})}),
  uploadPractice:(id:string,file:File)=>call<any>(`/api/chapters/${id}/practice/attachments`,{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-Filename':encodeURIComponent(file.name)},body:file}),
  practice:(id:string,content:object,attachmentIds:string[])=>call<any>(`/api/chapters/${id}/practice`,{method:'POST',body:JSON.stringify({content,attachmentIds})}),
  uploadCapstone:(id:string,file:File)=>call<any>(`/api/books/${id}/capstone/attachments`,{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-Filename':encodeURIComponent(file.name)},body:file}),
  capstone:(id:string,content:object,attachmentIds:string[])=>call<any>(`/api/books/${id}/capstone`,{method:'POST',body:JSON.stringify({content,attachmentIds})}),
  updateChapter:(id:string,body:object)=>call<import('../model/types').Chapter>(`/api/chapters/${id}`,{method:'PATCH',body:JSON.stringify(body)}),
  addChapter:(id:string,body:object)=>call<import('../model/types').Chapter>(`/api/books/${id}/chapters`,{method:'POST',body:JSON.stringify(body)}),
  deleteChapter:(id:string)=>call<void>(`/api/chapters/${id}`,{method:'DELETE'}),
};
