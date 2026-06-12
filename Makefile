.PHONY: demo test init clean

demo:           ## 六幕完整 demo（零依賴、離線）
	python3 run_demo.py

test:           ## 8 個平台測試
	python3 tests/test_platform.py

init:           ## 建 DB＋註冊角色
	python3 -m agentops init

clean:          ## 清 demo 產物
	rm -f db/demo.db* runs/run*.json runs/run*.md runs/run*.diff
