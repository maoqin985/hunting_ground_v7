"""V7.0 startup script — launches web server."""
import uvicorn, logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO)
from web.server import create_app
app = create_app()
uvicorn.run(app, host='0.0.0.0', port=8501, log_level='info')
