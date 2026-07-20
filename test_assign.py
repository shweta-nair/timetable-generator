from app import app, db
import logging

logging.basicConfig(level=logging.INFO)

app.config['WTF_CSRF_ENABLED'] = False
app.config['LOGIN_DISABLED'] = True

with app.test_client() as client:
    with client.session_transaction() as sess:
        sess['tabs'] = {'test_tab': {'role': 'admin', 'user_id': 'admin'}}
        sess['logged_in_tab_id'] = 'test_tab'

    import app as main_app
    main_app.is_admin = lambda: True

    print("POST /admin/assign-subjects")
    res = client.post('/admin/assign-subjects', follow_redirects=True)
    print("Final URL:", res.request.url)
    if 'Completed with errors' in res.data.decode('utf-8'):
        print("ERRORS FOUND in flash messages! Wrote to errors.txt.")
        with open('errors.txt', 'w') as f:
            for line in res.data.decode('utf-8').split('\n'):
                if 'Completed with errors' in line:
                    f.write(line)
    else:
        print("Success flash message found?" , 'Subject Assignment + Timetable Generation complete!' in res.data.decode('utf-8'))
        
    from database import Timetable
    with app.app_context():
        tt_count = Timetable.query.count()
        print(f"Generated {tt_count} timetable rows.")
