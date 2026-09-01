cd backend
uvicorn api_server_mixed:app --reload                   

cd frontend
python -m http.server 5500

mozila/chrome
http://localhost:5500/mixed_production_planner_dashboard.html
