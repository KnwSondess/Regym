import numpy as np
import matplotlib.pyplot as plt 
import matplotlib.animation as anim
import os 

def save_traj_with_graph(trajectory, data, episode=0, actor_idx=0, path='./', divider=10, colors=['blue', 'green', 'red', 'yellow', 'orange', 'black', 'grey'], markers=['o', 's', 'p', 'P', '*', 'h', 'H']):
    path = './'+path
    fig = plt.figure()
    imgs = []
    gd = [[]]*len(data)
    for idx, state in enumerate(trajectory):
        if state.shape[-1] != 3:
            # handled Stacked images...
            img_ch = 3
            if state.shape[-1] % 3: img_ch = 1
            per_image_first_channel_indices = range(0,state.shape[-1]+1,img_ch)
            ims = [ state[...,idx_begin:idx_end] for idx_begin, idx_end in zip(per_image_first_channel_indices,per_image_first_channel_indices[1:])]
            for img in ims:
                imgs.append(img.squeeze())
                for didx, d in enumerate(data):
                    gd[didx].append(d[idx])
        else:
            imgs.append(state)
            for didx, d in enumerate(data):
                gd[didx].append(d[idx])

    gifimgs = []
    for idx, img in enumerate(imgs):
        if idx%divider: continue
        plt.subplot(211)
        gifimg = plt.imshow(img, animated=True)
        ax = plt.subplot(212)
        
        lines = []
        for didx, d in enumerate(gd):
            x = np.arange(0,idx,1)
            y = np.asarray(d[:idx])
            ax.set_xlim(left=0,right=idx+10)
            line = ax.plot(x, y, color=colors[didx%len(colors)], marker=markers[didx%len(markers)], linestyle='dashed',linewidth=2, markersize=10)
            lines.append(line)
        
        gifimgs.append([gifimg])#+line)
        
    gif = anim.ArtistAnimation(fig, gifimgs, interval=200, blit=True, repeat_delay=None)
    path = os.path.join(path, f'./traj_ep{episode}_actor{actor_idx}.mp4')
    try:
        gif.save(path, dpi=None, writer='imagemagick')
    except Exception as e:
        print(f"Issue while saving trajectory: {e}")
    #plt.show()
    plt.close(fig)
    #print(f"GIF Saved: {path}")
