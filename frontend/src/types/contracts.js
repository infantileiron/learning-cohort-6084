/** @typedef {{id:number,code:string,learner:string,occurred_at:string,course:string,note:string}} Event */
/** @typedef {{id:number,name:string,start_week:string,end_week:string,as_of:string,horizon:number,state:string,result:object}} Cohort */
/** @typedef {{id:number,cohort_id:number,cohort_week:string,week_index:number,members:string[],note:string,status:string}} Intervention */

/** @typedef {{id:number,name:string,status:string,courses:string[],prerequisites:Object<string,string[]>,checksum:string,published_at:string}} RuleVersion */
/** @typedef {{id:number,rule_version_id:number,state:string,as_of:string,phase:string,progress:number,total:number,attempt:number,error:string,result:object}} RuleRun */
/** @typedef {{public_id:string,learner:string,rule_version_id:number,run_id:number,status:string,basis:object,issued_at:string,revoked_at:string,revoke_reason:string}} Certificate */
