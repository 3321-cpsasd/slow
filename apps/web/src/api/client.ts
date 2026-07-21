const BASE = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000';

async function call<T>(path:string, init?:RequestInit):Promise<T>{
  const response = await fetch(`${BASE}${path}`, {headers:{'Content-Type':'application/json'}, ...init});
  const text = await response.text();
  const payload = text ? JSON.parse(text) : undefined;
  if(!response.ok) throw new Error(payload.error || '请求失败');
  return payload as T;
}

export const api = {
  bootstrap:()=>call<import('../model/types').Bootstrap>('/api/bootstrap'),
  createPlan:(body:object)=>call<import('../model/types').Series>('/api/plans',{method:'POST',body:JSON.stringify(body)}),
  series:(id:string)=>call<import('../model/types').Series>(`/api/series/${id}`),
  chapter:(id:string)=>call<import('../model/types').Chapter>(`/api/chapters/${id}/generate`,{method:'POST'}),
  section:(id:string)=>call<import('../model/types').Section>(`/api/sections/${id}`),
  generateSection:(id:string)=>call<import('../model/types').Section>(`/api/sections/${id}/generate`,{method:'POST'}),
  quiz:(id:string,quizSetId:string,answers:number[][])=>call<any>(`/api/sections/${id}/quiz`,{method:'POST',body:JSON.stringify({quizSetId,answers})}),
  ask:(id:string,blockId:string,question:string,threadId?:string,forceRelation?:'follow_up'|'new_question')=>call<any>(`/api/sections/${id}/ask`,{method:'POST',body:JSON.stringify({blockId,question,threadId,forceRelation})}),
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
