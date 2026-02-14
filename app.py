import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# Config - Use Postgres on Render, or SQLite locally
app.config['SECRET_KEY'] = 'mysecretkey123' # Change this in production
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///local_db.sqlite')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)

# --- MODELS ---

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)

class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    # A group represents a specific "Sport" context (e.g., "Office Chess")

class GroupMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    elo_rating = db.Column(db.Integer, default=1200)

    user = db.relationship('User', backref='memberships')
    group = db.relationship('Group', backref='members')

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    winner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    loser_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    score = db.Column(db.String(50)) # e.g., "21-19"
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    winner = db.relationship('User', foreign_keys=[winner_id])
    loser = db.relationship('User', foreign_keys=[loser_id])

# --- HELPER: ELO CALCULATION ---
def calculate_elo(winner_elo, loser_elo):
    K = 32
    expected_winner = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_loser = 1 / (1 + 10 ** ((winner_elo - loser_elo) / 400))

    new_winner_elo = winner_elo + K * (1 - expected_winner)
    new_loser_elo = loser_elo + K * (0 - expected_loser)

    return round(new_winner_elo), round(new_loser_elo)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username taken'}), 400
    hashed_pw = generate_password_hash(data['password'], method='pbkdf2:sha256')
    new_user = User(username=data['username'], password=hashed_pw)
    db.session.add(new_user)
    db.session.commit()
    return jsonify({'message': 'Created'})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(username=data['username']).first()
    if user and check_password_hash(user.password, data['password']):
        login_user(user)
        return jsonify({'message': 'Logged in', 'username': user.username, 'id': user.id})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/logout')
@login_required
def logout():
    logout_user()
    return jsonify({'message': 'Logged out'})

@app.route('/api/groups', methods=['GET', 'POST'])
@login_required
def groups():
    if request.method == 'POST':
        # Create new group, make creator admin
        data = request.json
        new_group = Group(name=data['name'])
        db.session.add(new_group)
        db.session.commit()

        member = GroupMember(user_id=current_user.id, group_id=new_group.id, is_admin=True)
        db.session.add(member)
        db.session.commit()
        return jsonify({'message': 'Group created'})

    # List my groups
    memberships = GroupMember.query.filter_by(user_id=current_user.id).all()
    group_list = []
    for m in memberships:
        group_list.append({
            'id': m.group.id,
            'name': m.group.name,
            'is_admin': m.is_admin
        })
    return jsonify(group_list)

@app.route('/api/groups/<int:group_id>/details')
@login_required
def group_details(group_id):
    # Security check: is user in group?
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership:
        return jsonify({'error': 'Unauthorized'}), 403

    # 1. Matches
    matches_data = []
    matches = Match.query.filter_by(group_id=group_id).order_by(Match.timestamp.desc()).all()
    for m in matches:
        matches_data.append({
            'winner': m.winner.username,
            'loser': m.loser.username,
            'score': m.score,
            'date': m.timestamp.strftime('%Y-%m-%d')
        })

    # 2. Leaderboard (Elo Descending)
    members = GroupMember.query.filter_by(group_id=group_id).order_by(GroupMember.elo_rating.desc()).all()
    leaderboard_data = []
    members_list = [] # For member tab

    for m in members:
        user_obj = {'username': m.user.username, 'elo': m.elo_rating, 'is_admin': m.is_admin, 'id': m.user_id}
        leaderboard_data.append(user_obj)
        members_list.append(user_obj)

    return jsonify({
        'matches': matches_data,
        'leaderboard': leaderboard_data,
        'members': members_list,
        'is_user_admin': membership.is_admin,
        'group_name': membership.group.name
    })

@app.route('/api/groups/<int:group_id>/add_match', methods=['POST'])
@login_required
def add_match(group_id):
    # Check admin
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership or not membership.is_admin:
        return jsonify({'error': 'Admin only'}), 403

    data = request.json
    winner_id = int(data['winner_id'])
    loser_id = int(data['loser_id'])
    score = data['score']

    # Update Elo
    winner_mem = GroupMember.query.filter_by(user_id=winner_id, group_id=group_id).first()
    loser_mem = GroupMember.query.filter_by(user_id=loser_id, group_id=group_id).first()

    new_w_elo, new_l_elo = calculate_elo(winner_mem.elo_rating, loser_mem.elo_rating)
    winner_mem.elo_rating = new_w_elo
    loser_mem.elo_rating = new_l_elo

    # Save Match
    match = Match(group_id=group_id, winner_id=winner_id, loser_id=loser_id, score=score)
    db.session.add(match)
    db.session.commit()

    return jsonify({'message': 'Match added'})

@app.route('/api/groups/<int:group_id>/add_member', methods=['POST'])
@login_required
def add_member(group_id):
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership or not membership.is_admin:
        return jsonify({'error': 'Admin only'}), 403

    username_to_add = request.json['username']
    user_to_add = User.query.filter_by(username=username_to_add).first()
    if not user_to_add:
        return jsonify({'error': 'User not found'}), 404

    if GroupMember.query.filter_by(user_id=user_to_add.id, group_id=group_id).first():
        return jsonify({'error': 'Already in group'}), 400

    new_mem = GroupMember(user_id=user_to_add.id, group_id=group_id)
    db.session.add(new_mem)
    db.session.commit()
    return jsonify({'message': 'Member added'})

@app.route('/api/groups/<int:group_id>/make_admin', methods=['POST'])
@login_required
def make_admin(group_id):
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership or not membership.is_admin:
        return jsonify({'error': 'Admin only'}), 403

    target_user_id = request.json['user_id']
    target_mem = GroupMember.query.filter_by(user_id=target_user_id, group_id=group_id).first()
    target_mem.is_admin = True
    db.session.commit()
    return jsonify({'message': 'Admin rights granted'})

# Create tables on startup
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
