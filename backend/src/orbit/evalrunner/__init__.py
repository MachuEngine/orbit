"""평가 러너 v0 (ADR-0009 §3, D3).

과제 데이터셋을 Sim 월드 포크 위에서 평가 대상(SUT)에 돌리고, 코드로 채점해 JSONL 결과를 남긴다.
통계(`stats`)는 결과 JSONL만 입력으로 받는다. 과제 데이터는 `orbit.evaldata`가 읽고 검증하고,
`task_source`가 러너 고유 검증을 더한다.
"""
