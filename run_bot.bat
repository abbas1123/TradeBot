@echo off
cd /d C:\Users\Abbbas\Desktop\TradeBot
.venv\Scripts\python.exe main.py --mode serve --source live --top 20 --leverage 2 --risk 2 --timeframe 1h --capital 1000 --report-min 30 --no-open > data_store\serve_demo.log 2>&1
